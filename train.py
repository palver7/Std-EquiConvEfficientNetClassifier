from efficientnet_pytorch.model import EfficientNet
import argparse
import logging
#import sagemaker_containers
import os
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.parallel
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision
import torchvision.models
import torchvision.transforms as transforms
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader, Subset
from PIL import Image
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
import time
from torch.utils.tensorboard import SummaryWriter

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def split_to_3datasets(FullDataset):
    
    trainval_idx, test_idx = train_test_split(np.arange(len(FullDataset.targets)),test_size=0.3, shuffle=True, stratify=FullDataset.targets)
    testset=Subset(FullDataset,test_idx)
    labels=[]
    for idx in trainval_idx:
        labels.append(FullDataset[idx][1])

    train_idx, valid_idx = train_test_split(trainval_idx,test_size=0.5, shuffle=True, stratify=labels)
    trainset = Subset(FullDataset, train_idx)
    validset = Subset(FullDataset, valid_idx)
    
    return trainset, validset, testset


class TransformDataset(Dataset):
    

    def __init__(self, dataset, transform=None, target_transform=None):

        """
        Args:
            json_file (string): Path to the json file with annotations.
            transform (callable, optional): Optional transform to be applied
                on an image.
            target_file (callable, optional): Optional transform to be applied
                on a map (edge and corner).    
        """

   
        self.images_data = dataset 
        self.transform = transform
        self.target_transform = target_transform

    def __len__(self):
        return len(self.images_data)

    def __getitem__(self, idx):
        
        image, label = self.images_data[idx]
        
        if self.transform is not None:
            image = self.transform(image)
        
        if self.target_transform is not None:
            label = self.target_transform(label)
                

        return image, label


def _train(args):

    
    """
    is_distributed = len(args.hosts) > 1 and args.dist_backend is not None
    logger.debug("Distributed training - {}".format(is_distributed))

    if is_distributed:
        # Initialize the distributed environment.
        world_size = len(args.hosts)
        os.environ['WORLD_SIZE'] = str(world_size)
        host_rank = args.hosts.index(args.current_host)
        os.environ['RANK'] = str(host_rank)
        dist.init_process_group(backend=args.dist_backend, rank=host_rank, world_size=world_size)
        logger.info(
            'Initialized the distributed environment: \'{}\' backend on {} nodes. '.format(
                args.dist_backend,
                dist.get_world_size()) + 'Current host rank is {}. Using cuda: {}. Number of gpus: {}'.format(
                dist.get_rank(), torch.cuda.is_available(), args.num_gpus))          
    """

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    logger.info("Device Type: {}".format(device))

    logger.info("Loading dataset from folder")
    img_size = EfficientNet.get_image_size(args.model_name)
    transform = transforms.Compose(
        [transforms.Resize((img_size,img_size)),
         transforms.ToTensor(),
         transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])])


    transformaugment = transforms.Compose([transforms.Resize((img_size,img_size)),
                                           transforms.ColorJitter(brightness=0.5,contrast=0.5,saturation=0.5,hue=0.5),
                                           transforms.RandomAffine(10,translate=(0.2,0.2)),
                                           transforms.RandomHorizontalFlip(0.5),
                                           transforms.ToTensor(),
                                           transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
                                           transforms.RandomErasing(value='random')])    
    #target_transform = transforms.Compose([transforms.Resize((224,224)),
    #                                       transforms.ToTensor()])     

    root = os.path.join(args.data_dir,"train")
    FullDataset = torchvision.datasets.ImageFolder(root, transform = None, target_transform = None)
    trainset,validset,testset = split_to_3datasets(FullDataset)

    trainset = TransformDataset(trainset, transform=transformaugment)
    train_loader = DataLoader(trainset, batch_size=args.batch_size,
                                               shuffle=True, num_workers=args.workers)
    
    validset = TransformDataset(validset, transform=transform)
    valid_loader = DataLoader(validset, batch_size=args.batch_size,
                                              shuffle=False, num_workers=args.workers)

    testset = TransformDataset(testset, transform=transform)
    test_loader = DataLoader(testset, batch_size=1,
                                              shuffle=False, num_workers=args.workers)                                          
    """
    root = 'val'
    validset = torchvision.datasets.ImageFolder(root, transform = transform, target_transform = None)
    valid_loader = DataLoader(validset, batch_size=1,
                                              shuffle=False, num_workers=args.workers)
    """                                          
    class_map = FullDataset.classes
    num_classes = len(FullDataset.classes)                                         

    logger.info("Model loaded")
    if(args.conv_type=='Std'):
        layerdict, offsetdict = None, None
    elif(args.conv_type=='Equi'):
        layerdict, offsetdict = torch.load('layertrain.pt'), torch.load('offsettrain.pt') 
    model = EfficientNet.from_pretrained(args.model_name,conv_type=args.conv_type,layerdict=layerdict,offsetdict=offsetdict)
    for param in model.parameters():
        param.requires_grad = False
    num_features = model._fc.in_features
    model._fc = nn.Linear(num_features,num_classes)

    if torch.cuda.device_count() > 1:
        logger.info("Gpu count: {}".format(torch.cuda.device_count()))
        model = nn.DataParallel(model)

    model = model.to(device)
    criterion = nn.CrossEntropyLoss().to(device)
    optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum)
    writer= SummaryWriter(log_dir="runs/{}".format(args.logdir),comment="visualising losses of training and validation")

    for epoch in range(1, args.epochs+1):
        epochtime1=time.time()
        # training phase
        running_loss = 0.0
        running_acc = 0.0
        for i, data in enumerate(train_loader):
            # get the inputs
            inputs, labels = data
            inputs, labels = inputs.to(device), labels.to(device)

            # zero the parameter gradients
            optimizer.zero_grad()

            # forward + backward + optimize
            model.train()
            outputs = model(inputs)
            loss = criterion(outputs,labels)
            _, preds = torch.max(outputs, 1)
            loss.backward()
            optimizer.step()

            # print statistics
            running_loss += loss.item()
            running_acc += torch.sum(preds == labels.data)
            """
            if i % args.batch_size == args.batch_size-1:  # print every args.batch_size mini-batches
                print('[%d, %5d] loss: %.3f Acc: %.3f' %
                      (epoch, i + 1, running_loss / args.batch_size, running_acc / args.batch_size))
                running_loss = 0.0
                running_acc = 0.0
            """    
        epoch_loss = running_loss / len(trainset)
        epoch_acc = running_acc.double() / len(trainset)    
        #print("train loss: %.3f train Acc: %.3f" %(epoch_loss, epoch_acc))
        writer.add_scalar("training_loss", epoch_loss,epoch)
        writer.add_scalar("training_acc", epoch_acc,epoch)
        # validation phase
        if(epoch%1==0):
            with torch.no_grad():
                running_loss = 0.0
                running_acc = 0.0
                for i, data in enumerate(valid_loader):
                    # get the inputs
                    inputs, labels = data
                    inputs, labels = inputs.to(device), labels.to(device)
                    model.eval()
                    outputs = model(inputs)
                    loss = criterion(outputs,labels)
                    _, preds = torch.max(outputs, 1)
                    
                    # print statistics
                    running_loss += loss.item()
                    running_acc += torch.sum(preds == labels.data)
                    """
                    if i % 1 == 0:  # print every 1 mini-batches
                        print('[%d, %5d] loss: %.3f Acc: %.3f' %
                        (epoch, i + 1, running_loss / 1, running_acc / 1))
                        running_loss = 0.0
                        running_acc = 0.0
                    """        
                epoch_loss = running_loss / len(validset)
                epoch_acc = running_acc.double() / len(validset)    
                #print("validation loss: %.3f validation Acc: %.3f" %(epoch_loss, epoch_acc))   
                writer.add_scalar("validation_loss", epoch_loss,epoch)
                writer.add_scalar("validation_acc", epoch_acc,epoch)
        epochtime2 = time.time()
    epochdiff = epochtime2-epochtime1             
    writer.close()   
    print ("time for 1 complete epoch: ", epochdiff)            
    print('Finished Training')
    """
    answer = input("Do you want to run inference on testset (y/n) ? ")
    if answer =='y':
        with torch.no_grad():
            for i,data in enumerate(test_loader):
                inputs, labels = data
                inputs, labels = inputs.to(device), labels.to(device)
                model.eval()
                outputs = model(inputs)
                preds = torch.topk(outputs, k=num_classes).indices.squeeze(0).tolist()        
                print('-----')
                for idx in preds:
                    category = class_map[idx]
                    prob = torch.softmax(outputs, dim=1)[0, idx].item()
                    print('{:<75} ({:.2f}%)'.format(category, prob*100))
    """

    return _save_model(model, args.model_dir)


def _save_model(model, model_dir):
    logger.info("Saving the model.")
    path = os.path.join(model_dir, 'model.pth')
    # recommended way from http://pytorch.org/docs/master/notes/serialization.html
    torch.save(model.cpu().state_dict(), path)


def model_fn(model_dir,model_name,num_classes):
    logger.info('model_fn')
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = EfficientNet.from_pretrained(model_name,conv_type='Std')
    for param in model.parameters():
        param.requires_grad = False
    num_features = model._fc.in_features
    model._fc = nn.Linear(num_features,num_classes)
    if torch.cuda.device_count() > 1:
        logger.info("Gpu count: {}".format(torch.cuda.device_count()))
        model = nn.DataParallel(model)

    with open(os.path.join(model_dir, 'model.pth'), 'rb') as f:
        model.load_state_dict(torch.load(f))
    return model.to(device)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--workers', type=int, default=2, metavar='W',
                        help='number of data loading workers (default: 2)')
    parser.add_argument('--epochs', type=int, default=1, metavar='E',
                        help='number of total epochs to run (default: 1)')
    parser.add_argument('--batch_size', type=int, default=4, metavar='BS',
                        help='batch size (default: 4)')
    parser.add_argument('--lr', type=float, default=0.001, metavar='LR',
                        help='initial learning rate (default: 0.001)')
    parser.add_argument('--momentum', type=float, default=0.9, metavar='M', help='momentum (default: 0.9)')
    parser.add_argument('--model-dir', type=str, default="")
    parser.add_argument('--data-dir', type=str, default="")
    parser.add_argument('--model-name', type=str,default="efficientnet-b0")
    parser.add_argument('--conv_type', type=str,default="Std")
    parser.add_argument('--logdir', type=str,default="EfficNetClassifierStd_30epochs", help='save directory for tensorboard event files')
    
    #parser.add_argument('--dist_backend', type=str, default='gloo', help='distributed backend (default: gloo)')
    #env = sagemaker_containers.training_env()
    #parser.add_argument('--hosts', type=list, default=env.hosts)
    #parser.add_argument('--current-host', type=str, default=env.current_host)
    #parser.add_argument('--model-dir', type=str, default=env.model_dir)
    #parser.add_argument('--data-dir', type=str, default=env.channel_input_dirs.get('training'))
    #parser.add_argument('--num-gpus', type=int, default=env.num_gpus)
    
    time1= time.time()
    _train(parser.parse_args())
    time2=time.time()
    diff = time2 - time1
    print("total execution time: ",diff," seconds")
    print("total execution time: ",diff/60," minutes")
    