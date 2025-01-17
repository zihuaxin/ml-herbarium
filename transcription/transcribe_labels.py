from functools import partial
import heapq
import os
import re
import shutil
import sys
from PIL import Image
import cv2
import pytesseract
from pytesseract import Output
import numpy as np
import multiprocessing as mp
import warnings
import pickle
from rapidfuzz import process
from tqdm import tqdm

cv2.setNumThreads(1)

### --------------------------------- Helper Functions --------------------------------- ###
def get_gt(fname, org_img_dir):
    gt_dir = org_img_dir + fname + "_gt.txt"
    if os.path.exists(gt_dir):
        gt = open(gt_dir).read().split("\n")
        gt = [i.lower() for i in gt if i]
        ground_truth = {s.split(": ")[0]: s.split(": ")[1] for s in gt}
        return ground_truth

    return None

def get_corpus_taxon(org_img_dir):
    # Mock corpus path:
    # corpus_dir = org_img_dir + "taxon" + "_corpus.txt"
    # corpus_full = open(corpus_dir).read().split("\n")

    # # Real corpus path:
    corpus_full = open("/projectnb/sparkgrp/ml-herbarium-grp/ml-herbarium-angeline1/ml-herbarium/corpus/corpus_taxon/corpus_taxon.txt").read().split("\n")
    corpus_full = [s.lower() for s in corpus_full]
    corpus_full = [s for s in corpus_full if s != ""]

    corpus_genus = [s.split(" ")[0] for s in corpus_full if len(s.split(" ")) > 1]
    corpus_species = [s.split(" ")[1] for s in corpus_full if len(s.split(" ")) > 1]
    
    corpus_full = list(set(corpus_full))
    corpus_genus = list(set(corpus_genus))
    corpus_species = list(set(corpus_species))

    return corpus_full, corpus_genus, corpus_species

def get_corpus(fname, org_img_dir, words = True):
    corpus_dir = org_img_dir + fname + "_corpus.txt"

    if words: 
        corpus = re.split("\n| ", open(corpus_dir).read()) # split on newline or space
    else:
        corpus = re.split("\n", open(corpus_dir).read()) # split on newline
    corpus = [s.lower() for s in corpus]
    corpus = [s for s in corpus if s != ""]
    corpus = list(set(corpus))

    return corpus

def getangle(img):
    # heavier preprocessing to blur, and threshold images (not to be saved) for reliable angle(skew) measurements
    blur = cv2.GaussianBlur(img, (5, 5), 0)
    ret, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # find the curves along the images with the same color (boundaries)
    contours, hierarchy = cv2.findContours(thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    largest_contour = contours[0]
    # traces the rectangle border of each image with a rectangle
    minAreaRect = cv2.minAreaRect(largest_contour)
    box = cv2.boxPoints(minAreaRect)
    # bitmap
    box = np.int0(box)
    angle = cv2.minAreaRect(largest_contour)[-1]
    #angle = - angle
    # dealing with strange errors, 95% accuracy 
    if angle < .09:
        angle = 0.0
    if angle > 5.0:
        angle /= 100
    if angle < -45:
        angle = -(90 + angle)
    
    #print(angle)
    return angle

def rotateimg(img):
    # gets the size of the entire image
    h,w = img.shape[:2]
    center = (w//2,h//2)
    # better than moments based deskewing because there is less error
    moment = cv2.getRotationMatrix2D(center, getangle(img), 1.0) # 1.0 bc do not need to grayscale twice
    # bicubic interpolation bc smoother than bilinear/K-nearest neighbors, interpolates with four kernels, each w/2 and h/2
    rotated = cv2.warpAffine(img, moment, (w,h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE) 
    return rotated

def get_img(image_path):
    warnings.filterwarnings("error")
    try:
        img = np.array(Image.open(image_path))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        kernel = np.ones((1, 1), np.uint8)
        img = cv2.dilate(img, kernel, iterations=1)
        img = cv2.erode(img, kernel, iterations=1)
        img = cv2.adaptiveThreshold(img,255,cv2.ADAPTIVE_THRESH_MEAN_C,cv2.THRESH_BINARY,51,14)
        # img = rotateimg(img)
        img = np.array(img)
    except:
        warnings.filterwarnings("default")
        return {}, image_path
    warnings.filterwarnings("default")
    return {image_path.split("/")[-1][:-4]: img}, None

def get_imgs(imgs, num_threads):
    imgs_out = {}
    failures = []

    print("\nGetting original images and preprocessing...")
    print("Starting multiprocessing...")
    pool = mp.Pool(min(num_threads, len(imgs)))
    for item, error in tqdm(pool.imap(get_img, imgs), total=len(imgs)):
        imgs_out.update(item)
        if error:
            failures.append(error)
    pool.close()
    pool.join()
    for f in failures:
        print("Failed to get image: "+f)
    print("Done.\n")

    return imgs_out

def has_y_overlap(y1, y2, h1, h2):
    if y1 < y2 and y2 < y1 + h1:
        return True
    elif y2 < y1 and y1 < y2 + h2:
        return True
    else:
        return False

def find_idx_nearby_text(ocr_results, img_name, result_idx):
    results = ocr_results[img_name]
    text = results["text"][result_idx]
    x = results["left"][result_idx]
    y = results["top"][result_idx]
    w = results["width"][result_idx]
    h = results["height"][result_idx]
    xmargin = 4*(w/len(text))
    for i in range(len(results["text"])):
        if i != result_idx:
            x2 = results["left"][i]
            y2 = results["top"][i]
            w2 = results["width"][i]
            h2 = results["height"][i]
            if has_y_overlap(y, y2, h, h2) and ((x+xmargin+w) > x2 or (x2+xmargin+w2) > x):
                return i
    return None

def words_to_lines(ocr_results, x_margin):
    lines = {}
    for img_name,results in ocr_results.items():
        lines[img_name] = []
        for i in range(0, len(results["text"])):
            x = results["left"][i]
            y = results["top"][i]
            w = results["width"][i]
            h = results["height"][i]
            for j in range(0, len(results["text"])):
                if i != j:
                    x2 = results["left"][j]
                    y2 = results["top"][j]
                    w2 = results["width"][j]
                    h2 = results["height"][j]
                    if has_y_overlap(y, y2, h, h2) and ((x+x_margin+w) > x2 or (x2+x_margin+w2) > x):
                        lines[img_name].append([i, j])
    return lines # Returns a dictionary of lines, where each line is a list of indices of words in the image from the ocr_results dictionary

def get_syn_dict():
    print("Getting synonym dictionary...")
    from synonym.generate_syn import main as generate_syn
    syn_dic_dir = '/projectnb/sparkgrp/ml-herbarium-grp/ml-herbarium-data/synonym-matching/output/syn_pure.pkl'

    if not os.path.exists(syn_dic_dir):
        generate_syn()

    with open(syn_dic_dir, 'rb') as f:
        syn_dict = pickle.load(f)
    
    print("Done.\n")

    return syn_dict


### --------------------------------- Import data & process --------------------------------- ###
def import_process_data(org_img_dir, num_threads):
    imgs = sorted(os.listdir(org_img_dir))
    imgs = [org_img_dir + img for img in imgs if img[-4:] == ".jpg"]
    imgs = get_imgs(imgs, num_threads)
    taxon_corpus_full, corpus_genus, corpus_species = get_corpus_taxon(org_img_dir)
    geography_corpus_words = get_corpus("geography", org_img_dir, words = True)
    geography_corpus_full = get_corpus("geography", org_img_dir, words = False)
    taxon_gt_txt = get_gt("taxon", org_img_dir)
    geography_gt_txt = get_gt("geography", org_img_dir)
    
    return imgs, geography_corpus_words, geography_corpus_full, taxon_gt_txt, geography_gt_txt, taxon_corpus_full


### --------------------------------- Optical character recognition --------------------------------- ###
def run_ocr(img_name, imgs, config):
    results = pytesseract.image_to_data(imgs[img_name], output_type=Output.DICT, config=config, lang="eng")
    return {img_name: results}

def ocr(imgs, num_threads):
    ocr_results = {}
    pytesseract.pytesseract.tesseract_cmd="/share/pkg.7/tesseract/4.1.3/install/bin/tesseract"
    tessdatapath = os.path.expanduser("~/ml-herbarium/transcription/handwriting_tesseract_training/tessdata")
    tessdata_dir_config = r'--tessdata-dir "{}"'.format(tessdatapath)
    print("Running OCR on images using Tesseract "+str(pytesseract.pytesseract.get_tesseract_version())+" ...")
    print("Starting multiprocessing...")
    pool = mp.Pool(min(num_threads, len(imgs)))
    func = partial(run_ocr, imgs=imgs, config=tessdata_dir_config)
    for item in tqdm(pool.imap(func, imgs), total=len(imgs)):
        ocr_results.update(item)
    pool.close()
    pool.join()
    print("Done.\n")

    return ocr_results

### --------------------------------- OCR debug output --------------------------------- ###
def generate_debug_output(img_name, ocr_results, imgs, org_img_dir, output_dir):
    results = ocr_results[img_name]
    debug_image = imgs[img_name]
    debug_image = cv2.cvtColor(debug_image, cv2.COLOR_GRAY2RGB)
    orig_image = cv2.imread(org_img_dir+img_name+".jpg")
    for i in range(0, len(results["text"])):
        x = results["left"][i]
        y = results["top"][i]
        
        w = results["width"][i]
        h = results["height"][i]
        text = results["text"][i]
        conf = int(results["conf"][i])
        if conf > 15:
            text = "".join([c if ord(c) < 128 else "" for c in text]).strip()
            cv2.rectangle(debug_image, (x, y), (x + w, y + h), (0, 255, 0), 2)
            cv2.putText(debug_image, text, (x, y - 25), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 200), 2)
            cv2.putText(debug_image, "Conf: "+str(conf), (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 200), 2)
            cv2.rectangle(orig_image, (x, y), (x + w, y + h), (0, 255, 0), 2)
            cv2.putText(orig_image, text, (x, y - 25), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 200), 2)
            cv2.putText(orig_image, "Conf: "+str(conf), (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 200), 2)
            
        elif conf > 0:
            text = "".join([c if ord(c) < 128 else "" for c in text]).strip()
            cv2.rectangle(debug_image, (x, y), (x + w, y + h), (255, 150, 0), 2)
            cv2.putText(debug_image, text, (x, y - 25), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 200), 2)
            cv2.putText(debug_image, "Conf: "+str(conf), (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 200), 2)
            cv2.rectangle(orig_image, (x, y), (x + w, y + h), (255, 150, 0), 2)
            cv2.putText(orig_image, text, (x, y - 25), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 200), 2)
            cv2.putText(orig_image, "Conf: "+str(conf), (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 200), 2)
            
    cv2.imwrite(output_dir+"/debug/"+img_name+".png", debug_image)
    cv2.imwrite(output_dir+"/debug/"+img_name+"_orig"+".png", orig_image)

def ocr_debug(ocr_results, output_dir, imgs, org_img_dir):
    if not os.path.exists(output_dir+"/debug/"):
        os.makedirs(output_dir+"/debug/")
    print("Generating debug outputs...")
    print("Starting multiprocessing...")
    pool = mp.Pool(min(len(ocr_results), mp.cpu_count()))
    func = partial(generate_debug_output, ocr_results=ocr_results, imgs=imgs, org_img_dir=org_img_dir, output_dir=output_dir)
    for item in tqdm(pool.imap(func, ocr_results), total=len(ocr_results)):
        pass
    pool.close()
    pool.join()        
    print("Done.\n")


# ### --------------------------------- Match words to corpus --------------------------------- ###
# def match_words_to_corpus(ocr_results, name, corpus_words, corpus_full, output_dir, debug=False):
#     cnt = 0
#     final = {}
#     print("Matching words to "+ name +" corpus...")
#     for img_name,results in tqdm(ocr_results.items(), total=len(ocr_results)):
#         if debug:
#             f = open(output_dir+"/debug/"+img_name+"_"+name+".txt", "w")
#         matched = False
#         matches = {}
#         for i in range(len(results["text"])):
#             text = results["text"][i].lower()
#             conf = int(results["conf"][i])
#             if conf > 30:    
#                 if debug:
#                     f.write("\n\nOCR output:\n")
#                     f.write(str(text)+"\n")
#                     f.write("Confidence: "+str(conf)+"\n")
#                 tmp = get_close_matches(text, corpus_words, n=1, cutoff=0.8)
#                 if debug:
#                     f.write("Close matches:\n")
#                     f.write(str(tmp)+"\n")
#                 if len(tmp) != 0:
#                     matches[i]=tmp[0]
#                     matched = True
#         if debug:
#             f.write("\n\nMatched words:\n")
#             f.write(str(matches)+"\n")
# 
#         matched_pairs = []
#         matched_pairs_matched = False
#         for m1 in matches.values():
#             for m2 in matches.values():
#                 if m1 != m2:
#                     tmp = get_close_matches(m1+" "+m2, corpus_full, n=1, cutoff=0.9)
#                     if len(tmp) != 0:
#                         matched_pairs.extend(tmp)
#                         matched_pairs_matched = True
#         if debug:
#             f.write("\n\nMatched pairs:\n")
#             f.write(str(matched_pairs)+"\n")
# 
#         if matched_pairs_matched:
#             final[img_name] = matched_pairs[0]
#             if debug:
#                 f.write("\n\n-------------------------\nFinal match (first element of list):\n")
#                 f.write(str(matched_pairs))
#                 f.write("\n-------------------------\n")
#         elif matched:
#             guesses = []
#             for i, m in matches.items():
#                 guess_idx = find_idx_nearby_text(ocr_results, img_name, i)
#                 if guess_idx != None:
#                     guesses.append("GUESS: "+ m + ocr_results[img_name]["text"][guess_idx])
#             if len(guesses) != 0:
#                 final[img_name] = guesses[0]
#                 if debug:
#                     f.write("\n\n-------------------------\nGuesses:\n")
#                     f.write(str(guesses))
#                     f.write("\n-------------------------\n")
# 
#         else: 
#             final[img_name]="NO MATCH"
#         if matched: cnt+=1
#     print("Done.\n")
#     return final


### --------------------------------- Match taxon to corpus --------------------------------- ###
def run_match_taxon(img, corpus_genus, corpus_species, output_dir, debug):
    img_name, results = img
    results_modified = [(results["conf"][i], results["text"][i]) for i in range(len(results["text"])) if int(results["conf"][i]) > 1 and len(results["text"][i]) > 1]
    mg = partial(match_genus, corpus_genus=corpus_genus)
    results_genus = list(map(mg, results_modified))
    ms = partial(match_species, corpus_species=corpus_species)
    results_species = list(map(ms, results_modified))
    # taking care of situation like this: this is result species: [['centeterius'], ['inceps'], ['smithsonianus'], 
    # ['constitutionis'], ['oto'], ['obsistens'], ['ochrea'], ['oto'], ['oto']]
    results_genus = list(set([x for x in results_genus if x != None and len(x) != 0]))
    results_species = list(set([x for x in results_species if x != None and len(x) != 0]))

    possible_species = []
    # first word in each list is a genus
    possible_species += [[x]+corpus_genus[x[1]] for x in results_genus]
    possible_genus= []
    # first word in each list is a species
    possible_genus += [[x]+corpus_species[x[1]] for x in results_species]

    matches_genus = []
    matches_species = []
    
    for a in results_species:
        for b in results_genus:
            if a[1] in corpus_genus[b[1]]:
                matches_species += [a]
                matches_genus += [b]
    
    for a in results_genus:
        for b in results_species:
            if a[1] in corpus_species[b[1]]:
                matches_genus += [a]
                matches_species += [b]
    
    matches_genus = list(set(matches_genus))
    matches_species = list(set(matches_species))

    # print("this is number", img_name )
    # print("this is result genus:", possible_genus)
    # print("this is result species:", possible_species)

    if debug:
        f = open(output_dir+"/debug/"+img_name+"_taxon.txt", "w")
        f.write("\n\n Results after OCR outputs: " + "\n")
        f.write("Genus: " + str(results_genus) + "\n")
        f.write("Species: " + str(results_species) + "\n")
        f.write("\n\n After using the possible genus/species dictionaries: " + "\n")
        f.write("Possible species: " + str(possible_species) + "\n")
        f.write("Possible genus: " + str(possible_genus) + "\n")
    
    if debug:
        f.write("\n\nMatched genera:\n")
        f.write(str(matches_genus)+"\n")
        f.write("\n\nMatched species:\n")
        f.write(str(matches_species)+"\n")

    # Structural pattern matching
    match [len(matches_genus), len(matches_species)]:
        case [0,0]:
            result = "NO MATCH"
        case [1,0]:
            result = matches_genus[0][1] + " " + "[NO MATCH SPECIES]"
            if debug:
                f.write("Single match for genus; no match for species.\n")
        case [0,1]:
            result = "[NO MATCH GENUS]" + " " + matches_species[0][1]
            if debug:
                f.write("No match for genus; single match for species.\n")
        case [1,1]:
            result = matches_genus[0][1] + " " + matches_species[0][1]
            if debug:
                f.write("Single match for genus and species.\n")
        case [1,_]:
            potential_taxa = []
            for species in matches_species:
                if species[1] in corpus_genus[matches_genus[0][1]]:
                    potential_taxa += [(matches_genus[0][1] + " " + species[1], species[0]+species[2])]
            if len(potential_taxa) == 0:
                result = matches_genus[0][1] + " " + "[NO MATCH SPECIES]"
            else:
                potential_taxa = sorted(potential_taxa, key=lambda x: x[1], reverse=True)
                result = potential_taxa[0][0]
                if debug:
                    f.write("Multiple matches for genus; no match for species.\n")
                    f.write("Potential taxa: " + str(potential_taxa) + "\n")
                    f.write("Final taxon: " + result + "\n")
        case [_,1]:
            potential_taxa = []
            for genus in matches_genus:
                if genus[1] in corpus_species[matches_species[0][1]]:
                    potential_taxa += [(genus[1] + " " + matches_species[0][1], genus[0]+genus[2])]
            if len(potential_taxa) == 0:
                result = "[NO MATCH GENUS]" + " " + matches_species[0][1]
            else:
                potential_taxa = sorted(potential_taxa, key=lambda x: x[1], reverse=True)
                result = potential_taxa[0][0]
                if debug:
                    f.write("No match for genus; multiple matches for species.\n")
                    f.write("Potential taxa: " + str(potential_taxa) + "\n")
                    f.write("Final taxon: " + result + "\n")
        case [_,_]:
            potential_taxa = []
            for genus in matches_genus:
                for species in matches_species:
                    if species[1] in corpus_genus[genus[1]]:
                        potential_taxa += [(genus[1] + " " + species[1], genus[0] + genus[2] + species[0] + species[2])]
            if len(potential_taxa) == 0:
                result = "NO MATCH"
            else:
                potential_taxa = sorted(potential_taxa, key=lambda x: x[1], reverse=True)
                result = potential_taxa[0][0]
                if debug:
                    f.write("Multiple matches for genus and species.\n")
                    f.write("Potential taxa: " + str(potential_taxa) + "\n")
                    f.write("Final taxon: " + str(result) + "\n")
                
    if debug:
        f.write("========================================================\n")
        f.write("Final result for "+img_name+":\n")
        f.write(result+"\n")
    
    return {img_name: result}

def match_genus(n, corpus_genus):
    if len(n[1]) > 1:
        choice = process.extractOne(n[1], list(corpus_genus.keys()), score_cutoff=91)
    if choice:
        text, similarity, _ = choice
        # return similartiy, matched genus, OCR confidence, OCR text
        return similarity, text, n[0], n[1]

def match_species(n, corpus_species):
    if len(n[1]) > 1:
        choice = process.extractOne(n[1], list(corpus_species.keys()), score_cutoff=91)
    if choice:
        text, similarity, _ = choice
        # return similartiy, matched species, OCR confidence, OCR text
        return similarity, text, n[0], n[1]

def match_taxon(ocr_results, taxon_corpus_full, corpus_genus, corpus_species, output_dir, debug=False):
    # corpus_genus: key is genus and value is a list of possible species 
    final = {}
    print("Matching words to taxon corpus...")
    print("Starting multiprocessing...")
    pool = mp.Pool(min(len(ocr_results), mp.cpu_count()))
    func = partial(run_match_taxon, corpus_genus=corpus_genus, corpus_species=corpus_species, output_dir=output_dir, debug=debug)
    for item in tqdm(pool.imap(func, ocr_results.items()), total=len(ocr_results)):
        final.update(item)
    pool.close()
    pool.join()
    print("Done.\n")
    return final




### --------------------------------- Determine which are same as ground truth/or just output results --------------------------------- ###
def determine_match(gt, final, fname, output_dir, syn_dict = None):
    f = open(output_dir+fname+"_results.txt", "w")
    cnt = 0
    pcnt = 0
    wcnt = 0
    ncnt = 0
    if gt != None:
        for img_name,final_val in final.items():
            if gt[img_name] == final_val:
                f.write(img_name+": "+final_val+"\n")
                cnt+=1
            elif "GUESS" in final_val:
                    if gt[img_name] == final_val.split("GUESS: ")[1]:
                        f.write(img_name+"––"+final_val+"\n")
                        cnt+=1
                    else:
                        f.write(img_name+"––"+final_val+"––EXPECTED:"+gt[img_name]+"\n")
                        ncnt+=1
            elif "[" in final_val:
                f.write(img_name+"––PARTIAL MATCH: "+final_val+"––EXPECTED:"+gt[img_name]+"\n")
                pcnt+=1
            else:
                if final_val=="NO MATCH":
                    f.write(img_name+": "+final_val+"––EXPECTED:"+gt[img_name]+"\n")
                    ncnt+=1
                else:
                    if syn_dict != None:
                        if (final_val in syn_dict) and (syn_dict[final_val] == gt[img_name]): # CRAFT output is a synonym
                            f.write(img_name+": " + syn_dict[final_val] + " by synonym" + "\n")
                            cnt += 1
                        elif (gt[img_name] in syn_dict) and (syn_dict[gt[img_name]] == final_val): # gt is a synonym
                            f.write(img_name+": " + final_val + " by synonym" + "\n")
                            cnt += 1
                        else:
                            f.write(img_name+"––WRONG: "+final_val+"––EXPECTED:"+gt[img_name]+"\n")
                            wcnt+=1
                    else:
                        f.write(img_name+"––WRONG: "+final_val+"––EXPECTED:"+gt[img_name]+"\n")
                        wcnt+=1

        print(fname+" acc: "+str(cnt)+"/"+str(len(final))+" = "+str((cnt/len(final))*100)+"%")
        print(fname+" no match: "+str(ncnt)+"/"+str(len(final))+" = "+str((ncnt/len(final))*100)+"%")
        print(fname+" wrong: "+str(wcnt)+"/"+str(len(final))+" = "+str((wcnt/len(final))*100)+"%"+"\n")
        f.write("\n"+fname+" acc: "+str(cnt)+"/"+str(len(final))+" = "+str((cnt/len(final))*100)+"%")
        f.write("\n"+fname+" no match: "+str(ncnt)+"/"+str(len(final))+" = "+str((ncnt/len(final))*100)+"%")
        f.write("\n"+fname+" wrong: "+str(wcnt)+"/"+str(len(final))+" = "+str((wcnt/len(final))*100)+"%"+"\n")
        f.close()
    else:
        for img_name,value in final.items():
            f.write(img_name+": "+value)
        f.close()

def main():
    org_img_dir = None
    output_dir = None
    num_threads = mp.cpu_count()
    debug = False
    args = sys.argv[1:]
    if len(args) == 0:
        print("\nUsage: python3 transcribe_labels.py <org_img_dir> [OPTIONAL ARGUMENTS]")
        print("\nOPTIONAL ARGUMENTS:")
        print("\t-o <output_dir>, --output <output_dir>")
        print("\t-n <num_threads>, --num-threads <num_threads> (Default: "+str(num_threads)+")")
        print("\t-d, --debug\n")
        sys.exit(1)
    if len(args) > 0:
        org_img_dir = args[0]
        if args.count("-o") > 0:
            output_dir = args[args.index("-o")+1]
        if args.count("--output") > 0:
            output_dir = args[args.index("--output")+1]
        if args.count("-n") > 0:
            num_threads = int(args[args.index("-n")+1])
        if args.count("--num-threads") > 0:
            num_threads = int(args[args.index("--num-threads")+1])
        if args.count("-d") > 0 or args.count("--debug") > 0:
            debug = True
    if org_img_dir[-1] != "/":
        org_img_dir += "/"
    if output_dir == None:
        if "/scraped-data/" in org_img_dir:
            output_dir = org_img_dir.replace('/scraped-data/', '/transcription-results/')
        else:
            output_dir = org_img_dir+"results/"
    imgs, geography_corpus_words, geography_corpus_full, taxon_gt_txt, geography_gt_txt, taxon_corpus_full = import_process_data(org_img_dir, num_threads)
    ocr_results = ocr(imgs, num_threads)
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir)
    if debug:
        ocr_debug(ocr_results, output_dir, imgs, org_img_dir)
    syn_dict = get_syn_dict()

    with open('/projectnb/sparkgrp/ml-herbarium-grp/ml-herbarium-data/corpus_taxon/output/possible_species.pkl', 'rb') as f:
        corpus_genus = pickle.load(f)
    with open('/projectnb/sparkgrp/ml-herbarium-grp/ml-herbarium-data/corpus_taxon/output/possible_genus.pkl', 'rb') as ff:
        corpus_species = pickle.load(ff)

    taxon_final = match_taxon(ocr_results, taxon_corpus_full, corpus_genus, corpus_species, output_dir, debug)
    # geography_final = match_words_to_corpus(ocr_results, "geography", geography_corpus_words, geography_corpus_full, output_dir, debug)
    determine_match(taxon_gt_txt, taxon_final, "taxon", output_dir, syn_dict)
    # determine_match(geography_gt_txt, geography_final, "geography", output_dir)

if __name__ == "__main__":
    main()
