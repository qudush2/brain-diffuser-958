import os
import sys
import numpy as np
import h5py
import scipy.io as spio
import nibabel as nib
import scipy as sp
from PIL import Image
import time
import torch
import clip
from torch.nn.functional import normalize

os.environ['TORCH_HOME'] = './cache/torch_cache'  
os.environ['CLIP_CACHE_DIR'] = './cache/clip_cache' 

import argparse
parser = argparse.ArgumentParser(description='Argument Parser')
parser.add_argument('-cap', '--cap_length', help='Caption length (short/long)', choices=["short", 'long', 'LLM'], required=True)
args = parser.parse_args()
cap_length = args.cap_length

start_time = time.time()

from scipy.stats import pearsonr,binom,linregress
import numpy as np
def pairwise_corr_all(ground_truth, predictions):
    r = np.corrcoef(ground_truth, predictions)#cosine_similarity(ground_truth, predictions)#
    r = r[:len(ground_truth), len(ground_truth):]  # rows: groundtruth, columns: predicitons
    #print(r.shape)
    # congruent pairs are on diagonal
    congruents = np.diag(r)
    #print(congruents)
    
    # for each column (predicition) we should count the number of rows (groundtruth) that the value is lower than the congruent (e.g. success).
    success = r < congruents
    success_cnt = np.sum(success, 0)
    
    # note: diagonal of 'success' is always zero so we can discard it. That's why we divide by len-1
    perf = np.mean(success_cnt) / (len(ground_truth)-1)
    p = 1 - binom.cdf(perf*len(ground_truth)*(len(ground_truth)-1), len(ground_truth)*(len(ground_truth)-1), 0.5)
    
    return perf, p


net_list = [
    ('inceptionv3','avgpool'),
    ('clip','final'),
    ('alexnet',2),
    ('alexnet',5),
    ('efficientnet','avgpool'),
    ('swav','avgpool')
    ]

feats_dir = f'data/eval_features_{cap_length}_captions/subj01'
test_dir = 'data/eval_features/test_images'

# if cap_length == 'preliminary':
#     num_test = 200  # For preliminary case, evaluate images 800-999
# else:
num_test = 982  # Original number of test images

distance_fn = sp.spatial.distance.correlation
pairwise_corrs = []

# Initialize results_dict before the network evaluation loop
results_dict = {
    'network_metrics': {
        'names': [net[0] + '_' + str(net[1]) for net in net_list],
        'pairwise_correlations': [],
        'distances': []
    },
    'image_metrics': {},  # Will be populated later
    'metadata': {
        'caption_length': cap_length,
        'num_test_images': num_test
    }
}

# Load test images correctly based on cap_length
# if cap_length == 'preliminary':
#     full_images = np.load('data/processed_data/subj01/nsd_train_stim_sub1.npy').astype(np.uint8)
#     test_images = full_images[800:1000]  # Take images 800-999 for testing
# else:
test_images = np.load('data/processed_data/subj01/nsd_test_stim_sub1.npy').astype(np.uint8)

for (net_name,layer) in net_list:
    file_name = '{}/{}_{}.npy'.format(test_dir,net_name,layer)
    gt_feat = np.load(file_name)
    
    file_name = '{}/{}_{}.npy'.format(feats_dir,net_name,layer)
    eval_feat = np.load(file_name)
    
    gt_feat = gt_feat.reshape((len(gt_feat),-1))
    eval_feat = eval_feat.reshape((len(eval_feat),-1))
    
    print(net_name,layer)
    if net_name in ['efficientnet','swav']:
        distance = np.array([distance_fn(gt_feat[i],eval_feat[i]) for i in range(num_test)]).mean()
        results_dict['network_metrics']['distances'].append(distance)
        print('distance: ', distance)
    else:
        pairwise_corrs.append(pairwise_corr_all(gt_feat[:num_test],eval_feat[:num_test])[0])
        print('pairwise corr: ',pairwise_corrs[-1])
        
from skimage.color import rgb2gray
from skimage.metrics import structural_similarity as ssim

# Existing SSIM and PixCorr calculations
ssim_list = []
pixcorr_list = []
for i in range(num_test):
    gen_image = Image.open(f'results/versatile_diffusion_from_{cap_length}_captions/subj01/{i}.png').resize((425,425))
    
    if cap_length == 'preliminary':
        gt_image = Image.fromarray(test_images[i])
    else:
        gt_image = Image.fromarray(test_images[i])
    
    gen_image = np.array(gen_image)/255.0
    gt_image = np.array(gt_image)/255.0
    pixcorr_res = np.corrcoef(gt_image.reshape(1,-1), gen_image.reshape(1,-1))[0,1]
    pixcorr_list.append(pixcorr_res)
    gen_image = rgb2gray(gen_image)
    gt_image = rgb2gray(gt_image)
    ssim_res = ssim(gen_image, gt_image, multichannel=True, gaussian_weights=True, sigma=1.5, use_sample_covariance=False, data_range=1.0)
    ssim_list.append(ssim_res)

ssim_list = np.array(ssim_list)
pixcorr_list = np.array(pixcorr_list)
print('PixCorr: {}'.format(pixcorr_list.mean()))
print('SSIM: {}'.format(ssim_list.mean()))

# Update results dictionary with SSIM and PixCorr
results_dict['image_metrics'].update({
    'pixel_correlation_mean': float(pixcorr_list.mean()),
    'ssim_mean': float(ssim_list.mean()),
    'pixel_correlation_all': pixcorr_list.tolist(),
    'ssim_all': ssim_list.tolist()
})

# Add before CLIP score calculation
def compute_clip_score(image1, image2, model, preprocess, device):
    """Compute CLIP similarity score between two images"""
    with torch.no_grad():
        # Preprocess images
        image1_input = preprocess(Image.fromarray(image1)).unsqueeze(0).to(device)
        image2_input = preprocess(Image.fromarray(image2)).unsqueeze(0).to(device)
        
        # Get CLIP image features
        image1_features = model.encode_image(image1_input)
        image2_features = model.encode_image(image2_input)
        
        # Normalize features
        image1_features = normalize(image1_features, dim=-1)
        image2_features = normalize(image2_features, dim=-1)
        
        # Compute similarity
        similarity = (image1_features @ image2_features.T).item()
        
    return similarity

# Then continue with the CLIP score calculation as before
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
clip_model, preprocess = clip.load("ViT-L/14", download_root='./cache/clip_cache', device=device)
clip_scores = []
for i in range(num_test):
    gen_image = Image.open(f'results/versatile_diffusion_from_{cap_length}_captions/subj01/{i}.png').resize((425,425))
    
    # Convert PIL Image to numpy array
    gen_image_array = np.array(gen_image)
    
    # Compute CLIP score
    clip_score = compute_clip_score(
        test_images[i],  # ground truth
        gen_image_array,  # generated image (already in uint8 format)
        clip_model,
        preprocess,
        device
    )
    clip_scores.append(clip_score)

clip_scores = np.array(clip_scores)
print('CLIP Score: {}'.format(clip_scores.mean()))

# Update results dictionary with CLIP scores
results_dict['image_metrics'].update({
    'clip_score_mean': float(clip_scores.mean()),
    'clip_scores_all': clip_scores.tolist()
})

end_time = time.time()
execution_time = end_time - start_time

# Now update the results dict with the execution time
results_dict['metadata']['execution_time_seconds'] = execution_time

# Save results
import json
output_dir = f'results/evaluation_metrics_{cap_length}_captions'
os.makedirs(output_dir, exist_ok=True)
output_file = os.path.join(output_dir, f'reconstruction_metrics_subj01.json')

with open(output_file, 'w') as f:
    json.dump(results_dict, f, indent=4)

print(f"\nResults saved to: {output_file}")
print(f"\nTotal execution time for evaluate reconstruction {cap_length}: {execution_time:.2f} seconds ({execution_time/60:.2f} minutes)")
print('='*50)
