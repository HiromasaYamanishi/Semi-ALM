import argparse
import os
import logging
import shutil
import time
import sys
import numpy as np
import math
from  tqdm import tqdm

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
from torch.optim.lr_scheduler import LambdaLR
import torch.optim
import torch.nn.functional as F
import model as models
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler

from utils.datasets import Get_Dataset
from utils.datasets import Get_fixmatch_Dataset, Get_sfda_Dataset
from fixmatch import test, save_checkpoint
from datetime import datetime

parser = argparse.ArgumentParser(description='Pedestrian Attribute Framework')
parser.add_argument('--experiment', default='peta', type=str, required=False, help='(default=%(default)s)')
parser.add_argument('--approach', default='inception_iccv', type=str, required=True, help='(default=%(default)s)')
parser.add_argument('--epochs', default=60, type=int, required=False, help='(default=%(default)d)')
parser.add_argument('--batch_size', default=16, type=int, required=False, help='(default=%(default)d)')
parser.add_argument('--lr', '--learning-rate', default=0.0001, type=float, required=False, help='(default=%(default)f)')
parser.add_argument('--optimizer', default='adam', type=str, required=False, help='(default=%(default)s)')
parser.add_argument('--momentum', default=0.9, type=float, required=False, help='(default=%(default)f)')
parser.add_argument('--weight_decay', default=0.0005, type=float, required=False, help='(default=%(default)f)')
parser.add_argument('--start-epoch', default=0, type=int, required=False, help='(default=%(default)d)')
parser.add_argument('--print_freq', default=100, type=int, required=False, help='(default=%(default)d)')
parser.add_argument('--save_freq', default=10, type=int, required=False, help='(default=%(default)d)')
parser.add_argument('--resume', default='', type=str, required=False, help='(default=%(default)s)')
parser.add_argument('--decay_epoch', default=(20,40), type=eval, required=False, help='(default=%(default)d)')
parser.add_argument('--prefix', default='', type=str, required=False, help='(default=%(default)s)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true', required=False, help='evaluate model on validation set')
parser.add_argument('--gpu-id', default='0', type=int,
                    help='id(s) for CUDA_VISIBLE_DEVICES')
parser.add_argument('--num-workers', type=int, default=4,
                    help='number of workers')
parser.add_argument('--num-labeled', type=int, default=4000,
                    help='number of labeled data')
parser.add_argument("--expand-labels", action="store_true",
                    help="expand labels to fit eval steps")
parser.add_argument('--arch', default='wideresnet', type=str,
                    choices=['wideresnet', 'resnext'],
                    help='dataset name')
parser.add_argument('--total-steps', default=2**20, type=int,
                    help='number of total steps to run')
parser.add_argument('--eval-step', default=1024, type=int,
                    help='number of eval steps to run')
parser.add_argument('--warmup', default=0, type=float,
                    help='warmup epochs (unlabeled data based)')
parser.add_argument('--wdecay', default=5e-4, type=float,
                    help='weight decay')
parser.add_argument('--nesterov', action='store_true', default=True,
                    help='use nesterov momentum')
parser.add_argument('--use-ema', action='store_true', default=True,
                    help='use EMA model')
parser.add_argument('--ema-decay', default=0.999, type=float,
                    help='EMA decay rate')
parser.add_argument('--mu', default=7, type=int,
                    help='coefficient of unlabeled batch size')
parser.add_argument('--lambda-u', default=1, type=float,
                    help='coefficient of unlabeled loss')
parser.add_argument('--T', default=1, type=float,
                    help='pseudo label temperature')
parser.add_argument('--threshold', default=0.95, type=float,
                    help='pseudo label threshold')
parser.add_argument('--out', default='result',
                    help='directory to output the result')
parser.add_argument('--seed', default=None, type=int,
                    help="random seed")
parser.add_argument("--amp", action="store_true",
                    help="use 16-bit (mixed) precision through NVIDIA apex AMP")
parser.add_argument("--opt_level", type=str, default="O1",
                    help="apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                    "See details at https://nvidia.github.io/apex/amp.html")
parser.add_argument("--local_rank", type=int, default=-1,
                    help="For distributed training: local_rank")
parser.add_argument('--no-progress', action='store_true',
                    help="don't use progress bar")
parser.add_argument('--target_label_file', type=str, default='./data/solider.txt')
parser.add_argument('--eval_label_file', type=str, default='./data/solider.txt')
parser.add_argument('--method_name', type=str, default='sfda')
#parser.add_argument('--unlabel_label_file', type=str, default='./data/solider.txt')
parser.add_argument('--root', type=str, default='.')
parser.add_argument('--lamb_intra', type=float, default=1.0)
parser.add_argument('--lamb_inter', type=float, default=-0.1)
parser.add_argument('--lamb_gent', type=float, default=1)
parser.add_argument('--epsilon', type=float, default=1e-5)
parser.add_argument('--ent_par', type=float, default=0.1)
parser.add_argument('--exp_id', type=str, default=None)
parser.add_argument('--max_size', type=int, default=None)
# Seed
np.random.seed(1)
torch.manual_seed(1)
if torch.cuda.is_available(): torch.cuda.manual_seed(1)
else: print('[CUDA unavailable]'); sys.exit()
best_accu = 0
best_acc = 0
EPS = 1e-12
logger = logging.getLogger(__name__)

#####################################################################################################
def get_cosine_schedule_with_warmup(optimizer,
                                    num_warmup_steps,
                                    num_training_steps,
                                    num_cycles=7./16.,
                                    last_epoch=-1):
    def _lr_lambda(current_step):
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))
        no_progress = float(current_step - num_warmup_steps) / \
            float(max(1, num_training_steps - num_warmup_steps))
        return max(0., math.cos(math.pi * num_cycles * no_progress))

    return LambdaLR(optimizer, _lr_lambda, last_epoch)

def interleave(x, size):
    s = list(x.shape)
    return x.reshape([-1, size] + s[1:]).transpose(0, 1).reshape([-1] + s[1:])

def Entropy(input_):
    bs = input_.size(0)
    epsilon = 1e-5
    entropy = -input_ * torch.log(input_ + epsilon)
    entropy = torch.sum(entropy, dim=1)
    return entropy 

def de_interleave(x, size):
    s = list(x.shape)
    return x.reshape([size, -1] + s[1:]).transpose(0, 1).reshape([-1] + s[1:])

def get_loss(logits, batch_size, criterion, targets_x, epoch, ):
    logits0 = de_interleave(logits[0], 2*args.mu+1)
    logits_x0 = logits0[:batch_size]
    logits_u_w0, logits_u_s0 = logits0[batch_size:].chunk(2)
    
    logits1 = de_interleave(logits[1], 2*args.mu+1)
    logits_x1 = logits1[:batch_size]
    logits_u_w1, logits_u_s1 = logits1[batch_size:].chunk(2)
    
    logits2 = de_interleave(logits[2], 2*args.mu+1)
    logits_x2 = logits2[:batch_size]
    logits_u_w2, logits_u_s2 = logits2[batch_size:].chunk(2)
    
    logits3 = de_interleave(logits[3], 2*args.mu+1)
    logits_x3 = logits2[:batch_size]
    logits_u_w3, logits_u_s3 = logits3[batch_size:].chunk(2)
    del logits
    
    #弱い拡張による画像とラベルのクロスエントロピー
    output = (logits_x0, logits_x1, logits_x2, logits_x3)
    if type(output) == type(()) or type(output) == type([]):
        loss_list = []
        # deep supervision
        for k in range(len(output)):
            out = output[k]
            loss_list.append(criterion.forward(torch.sigmoid(out), targets_x, epoch))
        Lx = sum(loss_list)
        # maximum voting
        output_x = torch.max(torch.max(torch.max(output[0],output[1]),output[2]),output[3])
    else:
        Lx = criterion.forward(torch.sigmoid(output), targets_x, epoch)
    return Lx

def main():
    global args, best_accu
    args = parser.parse_args()

    print('=' * 100)
    print('Arguments = ')
    for arg in vars(args):
        print('\t' + arg + ':', getattr(args, arg))
    print('=' * 100)
    if args.local_rank == -1:
        device = torch.device('cuda', args.gpu_id)
        args.world_size = 1
        args.n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device('cuda', args.local_rank)
        torch.distributed.init_process_group(backend='nccl')
        args.world_size = torch.distributed.get_world_size()
        args.n_gpu = 1
        
    args.device = device
    # Data loading code
    attr_nums = {'peta':35, 'pa100k':26}
    attr_num = attr_nums[args.experiment]
    
    target_dataset, test_dataset, description\
        = Get_sfda_Dataset(dataset=args.experiment,
                                target_label_txt=args.target_label_file,
                                test_label_txt=args.eval_label_file,
                                root=args.root,
                                max_size=args.max_size)
    train_sampler = RandomSampler if True else DistributedSampler

    target_trainloader = DataLoader(
        target_dataset,
        sampler=train_sampler(target_dataset),
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        drop_last=True)

    test_loader = DataLoader(
        test_dataset,
        sampler=SequentialSampler(test_dataset),
        batch_size=args.batch_size,
        num_workers=args.num_workers)
    labeled_epoch, unlabeled_epoch = 0, 0

    # create model
    model = models.__dict__[args.approach](pretrained=True, num_classes=attr_num)
    #print('model', model)
    # get the number of model parameters
    print('Number of model parameters: {}'.format(
        sum([p.data.nelement() for p in model.parameters()])))
    print('')

    # for training on multiple GPUs.
    # Use CUDA_VISIBLE_DEVICES=0,1 to specify which GPUs to use
    model = torch.nn.DataParallel(model).cuda()

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            best_accu = checkpoint['best_accu']
            model.load_state_dict(checkpoint['state_dict'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = False
    cudnn.deterministic = True

    # define loss function
    criterion = Weighted_BCELoss(args.experiment)

    #no_decay = ['bias', 'bn']
    fc = ['finalfc', 'fc']
    grouped_parameters = [
        {'params': [p for n, p in model.named_parameters() if not any(
            nd in n for nd in fc)], 'weight_decay': args.wdecay, 'lr': args.lr},
        {'params': [p for n, p in model.named_parameters() if any(
            nd in n for nd in fc)], 'weight_decay': 0.0, 'lr': 0}
    ]
    # grouped_parameters_name = [
    #     {'params': [n for n, p in model.named_parameters() if not any(
    #         nd in n for nd in fc)], 'weight_decay': args.wdecay, 'lr': args.lr*0.1},
    #     {'params': [n for n, p in model.named_parameters() if any(
    #         nd in n for nd in fc)], 'weight_decay': 0.0, 'lr': args.lr}
    # ]
    #print(grouped_parameters_name)
    optimizer = torch.optim.SGD(grouped_parameters, lr=args.lr,
                          momentum=0.9, nesterov=args.nesterov)

    args.epochs = math.ceil(args.total_steps / args.eval_step)
    scheduler = get_cosine_schedule_with_warmup(
        optimizer, args.warmup, args.total_steps)

    if args.evaluate:
        test(test_loader, model, attr_num, description)
        return
    print('start test')
    best_ma = 0
    exp_id = args.exp_id if args.exp_id is not None else datetime.now().strftime('%Y_%m_%d_%H:%M:%S')
    #test(test_loader, model, attr_num, description, best_ma, exp_id, args)
    for epoch in range(args.start_epoch, args.epochs):
        adjust_learning_rate(optimizer, epoch, args.decay_epoch)

        # train for one epoch
        train(target_trainloader, test_loader,
              model, optimizer, scheduler, epoch, criterion)
        #train(train_loader, model, criterion, optimizer, epoch)

        # evaluate on validation set
        # accu = validate(test_loader, model, criterion, epoch)

        ma=test(test_loader, model, attr_num, description, best_ma, exp_id, args)

        # remember best Accu and save checkpoint
        is_best = ma > best_ma
        best_ma = max(ma, best_ma)

        if is_best:
            save_checkpoint({
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'best_ma': best_ma,
            }, epoch+1, args.prefix, args, exp_id)
            
def obtain_label(model, target_trainloader,):
    model.eval()
    dset = {}
    with torch.no_grad():
        logits, confs, labels = [], [], []
        for input_x, _ in tqdm(target_trainloader):
            logit = model(input_x)
            output = torch.max(torch.max(torch.max(logit[0],logit[1]),logit[2]),logit[3])
            conf = torch.sigmoid(output)
            label = conf.ge(0.5).float()
            logits.append(output)
            confs.append(conf)
            labels.append(label)
            
    dset['logit'] = torch.cat(logits)
    dset['conf'] = torch.cat(confs)
    dset['label'] = torch.cat(labels)
    return dset
            
        

def train(target_trainloader, test_loader,
          model, optimizer,  scheduler, epoch, criterion):
    global best_acc, labeled_epoch, unlabeled_epoch
    test_accs = []
    end = time.time()
    
    if args.world_size > 1:
        labeled_epoch = 0
        unlabeled_epoch = 0
        target_trainloader.sampler.set_epoch(0)
        #labeled_trainloader.sampler.set_epoch(labeled_epoch)
        #unlabeled_trainloader.sampler.set_epoch(unlabeled_epoch)
        
    target_iter = iter(target_trainloader)
    #labeled_iter = iter(labeled_trainloader)
    #unlabeled_iter = iter(unlabeled_trainloader)
    
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    losses_x = AverageMeter()
    losses_u = AverageMeter()
    mask_probs = AverageMeter()
    top1 = AverageMeter()
    
    #dset = obtain_label(model, target_trainloader)
    #print(dset)
    model.train()
    if not args.no_progress:
        p_bar = tqdm(range(args.eval_step),)
    for batch_idx in range(args.eval_step):
        try:
            #inputs_x, targets_x = labeled_iter.next()
            # error occurs ↓
            (inputs_x, inputs_s),targets_x = next(target_iter)
        except:
            if args.world_size > 1:
                labeled_epoch += 1
                target_trainloader.sampler.set_epoch(labeled_epoch)
            target_iter = iter(target_trainloader)
            #inputs_x, targets_x = labeled_iter.next()
            # error occurs ↓
            inputs_x, inputs_s = next(target_iter)
        inputs_x = inputs_x.cuda(non_blocking=True)
        inputs_s = inputs_s.cuda(non_blocking=True)
        pred_3b, pred_4d, pred_5b, main_pred, pred_feature_3b, pred_feature_4d, pred_feature_5b, main_feat = model(input=inputs_x, return_feature=True)
        pred_3b_s, pred_4d_s, pred_5b_s, main_pred_s, pred_feature_3b_s, pred_feature_4d_s, pred_feature_5b_s, main_feat_s = model(input=inputs_s, return_feature=True)
        output = torch.max(torch.max(torch.max(pred_3b,pred_4d),pred_5b),main_pred) #[batch_size, attr_num]
        conf = torch.sigmoid(output) #[batch_size, attr_num]
        label = conf.ge(0.5).float() #[batch_size, attr_num]
        
        output_s = torch.max(torch.max(torch.max(pred_3b_s,pred_4d_s),pred_5b_s),main_pred)
        mask = ((conf>=0.8) | (conf<0.2)).to(int)
        classifier_loss = criterion.forward(torch.sigmoid(output_s), label, epoch, mask=mask)
        
        # Intra Class Tigtning 
        # Inter Class Separation
        intra_dist_all = torch.zeros(1).cuda()
        inter_dist_all = torch.zeros(1).cuda()
        for attr_ind in range(output.shape[1]):
            features_test = torch.cat([main_feat, pred_feature_3b[:, attr_ind, :].squeeze(1), \
                                pred_feature_4d[:, attr_ind, :].squeeze(1), pred_feature_5b[:, attr_ind, :].squeeze(1),], dim=1)
            pred = label[:, attr_ind]
            intra_dist = torch.zeros(1).cuda()
            inter_dist = torch.zeros(1).cuda()
            same_first = True
            diff_first = True
            cos = nn.CosineSimilarity(dim=1, eps=1e-6)
            for i in range(pred.size(0)):
                for j in range(i, pred.size(0)):
                    # dist = torch.norm(features_test[i] - features_test[j])
                    dist = 0.5 * (1 - cos(features_test[i].unsqueeze(0), features_test[j].unsqueeze(0)))
                    if pred[i].item() == pred[j].item():
                        if same_first:
                            intra_dist = dist.unsqueeze(0)
                            same_first = False
                        else:
                            intra_dist = torch.cat((intra_dist, dist.unsqueeze(0)))

                    else:
                        if diff_first:
                            inter_dist = dist.unsqueeze(0)
                            diff_first = False
                        else:
                            inter_dist = torch.cat((inter_dist, dist.unsqueeze(0)))
            intra_dist = torch.mean(intra_dist)
            inter_dist = torch.mean(inter_dist)
            intra_dist_all+=intra_dist
            inter_dist_all+=inter_dist
        intra_dist_all = intra_dist_all/output.shape[1]
        inter_dist_all = inter_dist_all/output.shape[1]
        classifier_loss += args.lamb_intra * intra_dist_all.squeeze()
        classifier_loss += args.lamb_inter * inter_dist_all.squeeze()
            
        # global entropy
        # local entropy
        #entropy_loss = torch.tensor(0.0).cuda()
        im_loss = torch.zeros(1).cuda()
        
        for attr_ind in range(output.shape[1]):
            softmax_out = conf[:, attr_ind]
            softmax_out = torch.stack([softmax_out, 1-softmax_out], dim=1) #[batch_size, 2]
            #print('softmax_out1', softmax_out.shape)
            entropy_loss = torch.mean(Entropy(softmax_out))
            msoftmax = softmax_out.mean(dim=0)
            gentropy_loss = torch.sum(-msoftmax * torch.log(msoftmax + args.epsilon))
            entropy_loss-=args.lamb_gent * gentropy_loss
            im_loss += entropy_loss * args.ent_par
        classifier_loss += im_loss.item()/output.shape[1]
        
        optimizer.zero_grad()
        classifier_loss.backward()
        optimizer.step()
        scheduler.step()
        model.zero_grad()
        #print(output.shape)
        #print(conf.shape)
        #print(label.shape)
        bs = targets_x.size(0)
        accu = accuracy(output.data, targets_x)
        losses.update(classifier_loss.data, bs)
        top1.update(accu, bs)

        optimizer.step()
        scheduler.step()

        #model.zero_grad()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if batch_idx % args.print_freq == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Accu {top1.val:.3f} ({top1.avg:.3f})'.format(
                      epoch, batch_idx, args.eval_step, batch_time=batch_time,
                      loss=losses, top1=top1))
        
        
    

def validate(val_loader, model, criterion, epoch):
    """Perform validation on the validation set"""
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    model.eval()

    end = time.time()
    for i, _ in tqdm(enumerate(val_loader)):
        input, target = _
        target = target.cuda(non_blocking=True)
        input = input.cuda(non_blocking=True)
        output = model(input)

        bs = target.size(0)

        if type(output) == type(()) or type(output) == type([]):
            loss_list = []
            # deep supervision
            for k in range(len(output)):
                out = output[k]
                loss_list.append(criterion.forward(torch.sigmoid(out), target, epoch))
            loss = sum(loss_list)
            # maximum voting
            output = torch.max(torch.max(torch.max(output[0],output[1]),output[2]),output[3])
        else:
            loss = criterion.forward(torch.sigmoid(output), target, epoch)

        # measure accuracy and record loss
        accu = accuracy(output.data, target)
        losses.update(loss.data, bs)
        top1.update(accu, bs)

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print('Test: [{0}/{1}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Accu {top1.val:.3f} ({top1.avg:.3f})'.format(
                      i, len(val_loader), batch_time=batch_time, loss=losses,
                      top1=top1))

    print(' * Accu {top1.avg:.3f}'.format(top1=top1))
    return top1.avg



class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

def adjust_learning_rate(optimizer, epoch, decay_epoch):
    lr = args.lr
    for epc in decay_epoch:
        if epoch >= epc:
            lr = lr * 0.1
        else:
            break
    print()
    print('Learning Rate:', lr)
    print()
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def accuracy(output, target):
    batch_size = target.size(0)
    attr_num = target.size(1)

    output = torch.sigmoid(output).cpu().numpy()
    output = np.where(output > 0.5, 1, 0)
    pred = torch.from_numpy(output).long()
    target = target.cpu().long()
    correct = pred.eq(target)
    correct = correct.numpy()

    res = []
    for k in range(attr_num):
        res.append(1.0*sum(correct[:,k]) / batch_size)
    return sum(res) / attr_num


class Weighted_BCELoss(object):
    """
        Weighted_BCELoss was proposed in "Multi-attribute learning for pedestrian attribute recognition in surveillance scenarios"[13].
    """
    def __init__(self, experiment):
        super(Weighted_BCELoss, self).__init__()
        self.weights = None
        if experiment == 'pa100k':
            self.weights = torch.Tensor([0.460444444444,
                                        0.0134555555556,
                                        0.924377777778,
                                        0.0621666666667,
                                        0.352666666667,
                                        0.294622222222,
                                        0.352711111111,
                                        0.0435444444444,
                                        0.179977777778,
                                        0.185,
                                        0.192733333333,
                                        0.1601,
                                        0.00952222222222,
                                        0.5834,
                                        0.4166,
                                        0.0494777777778,
                                        0.151044444444,
                                        0.107755555556,
                                        0.0419111111111,
                                        0.00472222222222,
                                        0.0168888888889,
                                        0.0324111111111,
                                        0.711711111111,
                                        0.173444444444,
                                        0.114844444444,
                                        0.006]).cuda()
        elif experiment == 'rap':
            self.weights = torch.Tensor([0.311434,
                                        0.009980,
                                        0.430011,
                                        0.560010,
                                        0.144932,
                                        0.742479,
                                        0.097728,
                                        0.946303,
                                        0.048287,
                                        0.004328,
                                        0.189323,
                                        0.944764,
                                        0.016713,
                                        0.072959,
                                        0.010461,
                                        0.221186,
                                        0.123434,
                                        0.057785,
                                        0.228857,
                                        0.172779,
                                        0.315186,
                                        0.022147,
                                        0.030299,
                                        0.017843,
                                        0.560346,
                                        0.000553,
                                        0.027991,
                                        0.036624,
                                        0.268342,
                                        0.133317,
                                        0.302465,
                                        0.270891,
                                        0.124059,
                                        0.012432,
                                        0.157340,
                                        0.018132,
                                        0.064182,
                                        0.028111,
                                        0.042155,
                                        0.027558,
                                        0.012649,
                                        0.024504,
                                        0.294601,
                                        0.034099,
                                        0.032800,
                                        0.091812,
                                        0.024552,
                                        0.010388,
                                        0.017603,
                                        0.023446,
                                        0.128917]).cuda()
        elif experiment == 'peta':
            self.weights = torch.Tensor([0.5016,
                                        0.3275,
                                        0.1023,
                                        0.0597,
                                        0.1986,
                                        0.2011,
                                        0.8643,
                                        0.8559,
                                        0.1342,
                                        0.1297,
                                        0.1014,
                                        0.0685,
                                        0.314,
                                        0.2932,
                                        0.04,
                                        0.2346,
                                        0.5473,
                                        0.2974,
                                        0.0849,
                                        0.7523,
                                        0.2717,
                                        0.0282,
                                        0.0749,
                                        0.0191,
                                        0.3633,
                                        0.0359,
                                        0.1425,
                                        0.0454,
                                        0.2201,
                                        0.0178,
                                        0.0285,
                                        0.5125,
                                        0.0838,
                                        0.4605,
                                        0.0124]).cuda()
        #self.weights = None

    def forward(self, output, target, epoch, mask=None):
        if self.weights is not None:
            cur_weights = torch.exp(target + (1 - target * 2) * self.weights)
            loss = cur_weights *  (target * torch.log(output + EPS)) + ((1 - target) * torch.log(1 - output + EPS))
        else:
            loss = target * torch.log(output + EPS) + (1 - target) * torch.log(1 - output + EPS)
        if mask is not None:
            loss = loss*mask
        return torch.neg(torch.mean(loss))

if __name__ == '__main__':
    main()
