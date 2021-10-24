# -*- encoding: utf-8 -*-
'''
@File    :   inference_cogview.py
@Time    :   2021/10/09 19:41:58
@Author  :   Ming Ding
@Contact :   dm18@mail.tsinghua.edu.cn
'''

# here put the import lib
import os
import sys
import math
import random
import torch
import argparse

from arguments import get_args
from model.cached_autoregressive_model import CachedAutoregressiveModel
from model.cached_object_model import CachedObjectModel
from model.ObjectModel import ObjectModel
from training import load_checkpoint, initialize_distributed, set_random_seed, prepare_tokenizer
from tokenization import get_tokenizer
from generation.sampling_strategies import BaseStrategy
from generation.autoregressive_sampling import filling_sequence
from generation.utils import timed_name, save_multiple_images, generate_continually


def main(args):
    initialize_distributed(args)
    tokenizer = prepare_tokenizer(args)
    # build model
    model = CachedObjectModel(args)
    if args.fp16:
        model = model.half()
    model = model.to(args.device)
    load_checkpoint(model, args)
    set_random_seed(args.seed)
    model.eval()

    # define function for each query
    invalid_slices = [slice(tokenizer.img_tokenizer.num_tokens, None)]
    strategy = BaseStrategy(invalid_slices,
                            temperature=args.temperature, topk=args.top_k)

    def process(raw_text):
        if args.with_id:
            query_id, raw_text = raw_text.split('\t')
        print('raw text: ', raw_text)
        raw_text = raw_text.split(' ')
        objects = raw_text[1:]
        seq = tokenizer.parse_query(f"[ROI1] {raw_text[0]}", img_size=args.img_size)
        for i in range(len(objects)//5):
            seq.append(tokenizer['POS0'])
            seq.extend(objects[i*5:i*5+4] + args.old_token_num)
            seq.extend(tokenizer.EncodeAsIds(objects[i*5 + 4]))
        seq.extend(tokenizer.parse_query('[BASE] [BOI1] [MASK]*1024', img_size=args.img_size))

        if len(seq) > 1271:
            raise ValueError('text too long.')
        # calibrate text length
        txt_len = seq.index(tokenizer['[BASE]'])
        log_attention_weights = torch.zeros(len(seq), len(seq),
                                            device=args.device, dtype=torch.half if args.fp16 else torch.float32)
        log_attention_weights[txt_len + 2:, 1:txt_len] = 1.8 if txt_len <= 10 else 1.4  # TODO args
        # generation
        seq = torch.cuda.LongTensor(seq, device=args.device)
        mbz = args.max_inference_batch_size
        assert args.batch_size < mbz or args.batch_size % mbz == 0
        output_list = []
        for tim in range(max(args.batch_size // mbz, 1)):
            output_list.append(
                filling_sequence(model, seq.clone(),
                                 batch_size=min(args.batch_size, mbz),
                                 strategy=strategy,
                                 log_attention_weights=log_attention_weights
                                 )
            )
        output_tokens = torch.cat(output_list, dim=0)
        # decoding
        imgs, txts = [], []
        for seq in output_tokens:
            txt_len = seq.index(tokenizer['[BASE]'])
            seq = seq[txt_len:]
            _, decoded_imgs = tokenizer.DecodeIds(seq.tolist())
            imgs.append(decoded_imgs[-1])  # only the last image (target)
        # save
        if args.with_id:
            full_path = os.path.join(args.output_path, query_id)
            os.makedirs(full_path, exist_ok=True)
            save_multiple_images(imgs, full_path, False)
        else:
            prefix = raw_text.replace('/', '')[:20]
            full_path = timed_name(prefix, '.jpg', args.output_path)
            save_multiple_images(imgs, full_path, True)

    os.makedirs(args.output_path, exist_ok=True)
    generate_continually(process, args.input_source)


if __name__ == "__main__":
    py_parser = argparse.ArgumentParser(add_help=False)
    py_parser.add_argument('--full-query', action='store_true')
    py_parser.add_argument('--img-size', type=int, default=256)
    ObjectModel.add_model_specific_args(py_parser)
    known, args_list = py_parser.parse_known_args()
    args = get_args(args_list)
    args = argparse.Namespace(**vars(args), **vars(known))

    with torch.no_grad():
        main(args)