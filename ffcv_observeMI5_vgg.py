# class-wise scores
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset
# from model.resnet import ResNet18
from model.vgg16 import VGG16
from model.TNet import TNet
import torch.nn.functional as F
import numpy as np
import math
import os
import random
import setproctitle
from ffcv.loader import Loader, OrderOption
from ffcv.transforms import ToTensor, ToDevice, Squeeze, RandomHorizontalFlip, RandomResizedCrop, RandomBrightness, RandomContrast, RandomSaturation
from ffcv.fields.decoders import IntDecoder, RandomResizedCropRGBImageDecoder
import argparse
from torchvision import transforms
import matplotlib.pyplot as plt
import gc
from torch.amp import autocast
import copy
from torch import Tensor
from typing import Callable
import concurrent.futures
import torch.multiprocessing as mp

proc_name = 'lover'
setproctitle.setproctitle(proc_name)


def get_acc(outputs, labels):
    """calculate acc"""
    _, predict = torch.max(outputs.data, 1)
    total_num = labels.shape[0] * 1.0
    correct_num = (labels == predict).sum().item()
    acc = correct_num / total_num
    return acc

def calculate_asr(model, dataloader, target_class, device):
    model.eval()
    attack_success_count = 0
    total_triggered_samples = 0

    with torch.no_grad():
        for X, y in dataloader:
            X, y = X.to(device), y.to(device)
            _, pred = model(X)
            _, predicted = torch.max(pred, 1)
            attack_success_count += (predicted == target_class).sum().item()
            total_triggered_samples += y.size(0)

    asr = 100 * attack_success_count / total_triggered_samples
    print(f"Attack Success Rate (ASR): {asr:.2f}%")
    return asr

# train one epoch
def train_loop(dataloader, model, loss_fn, optimizer, num_classes):
    size, num_batches = dataloader.batch_size, len(dataloader)
    model.train()
    epoch_acc = 0.0
    class_losses = torch.zeros(num_classes).to(next(model.parameters()).device)
    class_counts = torch.zeros(num_classes).to(next(model.parameters()).device)

    for batch, (X, y) in enumerate(dataloader):
        optimizer.zero_grad()
        _, pred = model(X)
        loss = loss_fn(pred, y)
        loss.backward()
        optimizer.step()
        epoch_acc += get_acc(pred, y)

        # 计算每个类别的损失
        for c in range(num_classes):
            mask = (y == c)
            if mask.sum() > 0:
                class_losses[c] += loss_fn(pred[mask], y[mask]).item() * mask.sum().item()
                class_counts[c] += mask.sum().item()

    avg_acc = 100 * (epoch_acc / num_batches)
    
    # 计算每个类别的平均损失
    class_losses = class_losses / class_counts
    class_losses = class_losses.cpu().numpy()

    print(f'Train acc: {avg_acc:.2f}%')
    for c in range(num_classes):
        print(f'Class {c} loss: {class_losses[c]:.4f}')

    return avg_acc, class_losses

def test_loop(dataloader, model, loss_fn):
    # Set the models to evaluation mode - important for batch normalization and dropout layers
    # Unnecessary in this situation but added for best practices
    model.eval()
    size = dataloader.batch_size
    num_batches = len(dataloader)
    total = size*num_batches
    test_loss, correct = 0, 0

    # Evaluating the models with torch.no_grad() ensures that no gradients are computed during test mode
    # also serves to reduce unnecessary gradient computations and memory usage for tensors with requires_grad=True
    with torch.no_grad():
        for X, y in dataloader:
            _, pred = model(X)
            test_loss += loss_fn(pred, y).item()
            correct += (pred.argmax(1) == y).type(torch.float).sum().item()

    test_loss /= num_batches
    correct /= total
    print(f"Test Error: \n Accuracy: {(100 * correct):>0.1f}%, Avg loss: {test_loss:>8f} \n")
    return test_loss, (100 * correct)

def compute_infoNCE(T, Y, Z, num_negative_samples=128):
    batch_size = Y.shape[0]
    # 随机选择负样本
    negative_indices = torch.randint(0, batch_size, (batch_size, num_negative_samples), device=Y.device)
    Z_negative = Z[negative_indices]
    
    # 计算正样本的得分
    t_positive = T(Y, Z).squeeze() # (batch_size, )
    # 计算负样本的得分
    Y_expanded = Y.unsqueeze(1).expand(-1, num_negative_samples, -1) # (batch_size, num_negative_samples, Y.dim)
    t_negative = T(Y_expanded.reshape(-1, Y.shape[1]), Z_negative.reshape(-1, Z.shape[1])) # (batch_size*num_negative_samples, )
    t_negative = t_negative.view(batch_size, num_negative_samples) # (batch_size, num_negative_samples)
    
    # 计算 InfoNCE loss
    logits = torch.cat([t_positive.unsqueeze(1), t_negative], dim=1).to(Y.device)  # (batch_size, num_negative_samples+1)
    log_sum_exp = logits.logsumexp(dim=1)
    # log_sum_exp = logits.exp().mean(dim=1).log()
    
    diffs = t_positive - log_sum_exp.mean() + math.log(num_negative_samples+1)
    loss = -diffs.mean()
    # loss = -diffs.mean()
    
    return loss, diffs

def dynamic_early_stop(M, patience=50, delta=1e-3):
    if len(M) > patience:
        recent_M = M[-patience:]
        if max(recent_M) - min(recent_M) < delta:
            return True
    return False

# 定义钩子函数
def hook(module, input, output):
    global last_conv_output
    last_conv_output = output

def estimate_mi(device, model, flag, sample_loader, EPOCHS=50, mode='infoNCE'):
    # LR = 1e-5
    initial_lr = 1e-4
    model.eval()
    if flag == 'inputs-vs-outputs':
        Y_dim, Z_dim = 512, 3072  # M的维度, X的维度
    elif flag == 'outputs-vs-Y':
        Y_dim, Z_dim = 10, 512  # Y的维度, M的维度
    else:
        raise ValueError('Not supported!')
    
    T = TNet(in_dim=Y_dim + Z_dim, hidden_dim=256).to(device)
    # T = torch.nn.DataParallel(T)  # 使用 DataParallel
    optimizer = torch.optim.AdamW(T.parameters(), lr=initial_lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=5, verbose=True)
    M = []

    # 注册钩子函数到最后一个 BasicBlock
    global last_conv_output
    last_conv_output = None
    hook_handle = model.layer5[-1].register_forward_hook(hook)

    sample_score_diffs_sum = torch.zeros(len(sample_loader)*sample_loader.batch_size).to(device)
    sample_score_diffs_count = torch.zeros(len(sample_loader)*sample_loader.batch_size).to(device)

    for epoch in range(EPOCHS):
        print(f"------------------------------- MI-Esti-Epoch {epoch + 1}-{mode} -------------------------------")
        L = []
        for batch, (X, _Y) in enumerate(sample_loader):
            X, _Y = X.to(device), _Y.to(device)
            with torch.no_grad():
                # with autocast(device_type="cuda"):
                    # Y = F.one_hot(_Y, num_classes=10)
                _, Y_predicted = model(X)
                if last_conv_output is None:
                    raise ValueError("last_conv_output is None. Ensure the hook is correctly registered and the model is correctly defined.")
                # 对 last_conv_output 进行全局平均池化
                M_output = F.adaptive_avg_pool2d(last_conv_output, 1)
                M_output = M_output.view(M_output.shape[0], -1)
            if flag == 'inputs-vs-outputs':
                X_flat = torch.flatten(X, start_dim=1)
                # print(f'X_flat.shape: {X_flat.shape}, M_output.shape: {M_output.shape}')
                loss, batch_scores = compute_infoNCE(T, M_output, X_flat)
            elif flag == 'outputs-vs-Y':
                Y = Y_predicted
                # Y = _Y
                loss, batch_scores = compute_infoNCE(T, Y, M_output)

            # 最后五轮更新样本得分取均值
            # if epoch >= EPOCHS - 5 or dynamic_early_stop(M, patience=45, delta=1e-2 if flag == 'inputs-vs-outputs' else 1e-3):
            start_idx = batch * sample_loader.batch_size
            end_idx = start_idx + X.shape[0]
            sample_score_diffs_sum[start_idx:end_idx] += batch_scores
            sample_score_diffs_count[start_idx:end_idx] += 1

            if math.isnan(loss.item()) or math.isinf(loss.item()):
                print(f"Skipping batch due to invalid loss: {loss.item()}")
                continue
            
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(T.parameters(), 5)
            optimizer.step()
            L.append(loss.item())
        
        if not L:
            M.append(float('nan'))
            continue
        
        avg_loss = np.mean(L)
        print(f'[{mode}] loss:', avg_loss, max(L), min(L))
        M.append(-avg_loss)  
        print(f'[{mode}] mi estimate:', -avg_loss)
        
        # Update the learning rate
        scheduler.step(avg_loss)
        
        if dynamic_early_stop(M, delta=1e-2 if flag == 'inputs-vs-outputs' else 1e-3):
            print(f'Early stopping at epoch {epoch + 1}')
            break
        
        # 清理缓存
        torch.cuda.empty_cache()
        gc.collect()
        average_sample_scores = sample_score_diffs_sum / sample_score_diffs_count

    return M, average_sample_scores.detach()


def plot_and_save_mi(mi_values_dict, mode, output_dir, epoch):
    plt.figure(figsize=(12, 8))
    for class_idx, mi_values in mi_values_dict.items():
        if isinstance(class_idx, str):  # 对于 '0_backdoor', '0_clean' 和 '0_sample'
            if "backdoor" in class_idx:
                label = "Class 0 Backdoor"
            elif "clean" in class_idx:
                label = "Class 0 Clean"
            elif "sample" in class_idx:
                label = "Class 0 Sample"
            mi_values_np = [v.cpu().numpy() if isinstance(v, torch.Tensor) else v for v in mi_values]
            plt.plot(range(1, len(mi_values_np) + 1), mi_values_np, label=label)
        else:
            epochs = range(1, len(mi_values) + 1)
            mi_values_np = mi_values.cpu().numpy() if isinstance(mi_values, torch.Tensor) else mi_values
            if int(class_idx) == 0:
                plt.plot(epochs, mi_values_np, label=f'Class {class_idx}')
            else:
                plt.plot(epochs, mi_values_np, label=f'Class {class_idx}', linestyle='--')
    
    plt.xlabel('Epochs')
    plt.ylabel('MI Value')
    plt.title(f'MI Estimation over Epochs ({mode}) - Training Epoch {epoch}')
    plt.legend()
    plt.grid(True)
    plt.savefig(os.path.join(output_dir, f'mi_plot_{mode}_epoch_{epoch}.png'))
    plt.close()

def plot_train_acc_ASR(train_accuracies, test_accuracies, ASR, epochs, outputs_dir):
    # Plot accuracy curves
    plt.figure(figsize=(10, 6))
    plt.plot(range(1, epochs + 1), train_accuracies, label='Train Accuracy')
    plt.plot(range(1, epochs + 1), test_accuracies, label='Test Accuracy')
    plt.plot(range(1, epochs + 1), ASR, label='ASR')
    plt.xlabel('Epoch')
    plt.ylabel('Accuracy')
    plt.title('Model Accuracy over Training')
    plt.legend()
    plt.grid(True)
    
    # Save the plot
    plt.savefig(outputs_dir + '/accuracy_plot.png')

def plot_train_loss_by_class(train_losses, epochs, num_classes, outputs_dir):
    plt.figure(figsize=(12, 8))
    for c in range(num_classes):
        plt.plot(range(1, epochs + 1), [losses[c] for losses in train_losses], label=f'Class {c}')
    plt.xlabel('Epoch')
    plt.ylabel('Training Loss')
    plt.title('Training Loss by Class over Epochs')
    plt.legend()
    plt.grid(True)
    plt.savefig(os.path.join(outputs_dir, 'train_loss_by_class_plot.png'))
    plt.close()

def compute_class_accuracy(model, dataloader, num_classes):
    model.eval()
    correct = [0] * num_classes
    total = [0] * num_classes
    
    with torch.no_grad():
        for X, y in dataloader:
            _, outputs = model(X)
            _, predicted = torch.max(outputs.data, 1)
            for i in range(len(y)):
                label = y[i].item()
                total[label] += 1
                if predicted[i] == label:
                    correct[label] += 1
    
    accuracies = [100 * correct[i] / total[i] if total[i] > 0 else 0 for i in range(num_classes)]
    return accuracies

def estimate_mi_wrapper(args):
    base_args, model_stat_dict, flag, class_idx, EPOCHS, mode = args
    device = torch.device(f"cuda:0" if torch.cuda.is_available() else "cpu")
    # if isinstance(class_idx, int) and int(class_idx) > 5:
    #     device = torch.device(f"cuda:1" if torch.cuda.is_available() else "cpu")
    # Data decoding and augmentation
    image_pipeline = [ToTensor(), ToDevice(device)]
    label_pipeline_sample = [ToTensor(), ToDevice(device)]
    pipelines_sample = {
        'image': image_pipeline,
        'label': label_pipeline_sample
    }
    
    sample_dataloader_path = f"{base_args.sample_data_path}_class_{class_idx}.beton"
    if class_idx == "0_backdoor":
        sample_dataloader_path = f"{base_args.sample_data_path}_class_0_backdoor.beton"
    elif class_idx == "0_clean":
        sample_dataloader_path = f"{base_args.sample_data_path}_class_0_clean.beton"
    elif class_idx == "0_sample":
        sample_dataloader_path = f"{base_args.sample_data_path}_class_0_sample.beton"
    sample_dataloader = Loader(sample_dataloader_path, batch_size=128, num_workers=16,
                               order=OrderOption.RANDOM, pipelines=pipelines_sample, seed=0)
    # if class_idx != "0_backdoor":
    sample_dataloader.indices = sample_dataloader.indices[:len(sample_dataloader.indices)//4]
    
    num_classes = 10
    model = VGG16(n_classes=num_classes)
    model.load_state_dict(model_stat_dict)
    model.to(device)
    
    return estimate_mi(device, model, flag, sample_dataloader, EPOCHS, mode)

def train(args, flag='inputs-vs-outputs', mode='infoNCE'):
    """ flag = inputs-vs-outputs or outputs-vs-Y """
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    batch_size = 512  
    learning_rate = 0.1

    # 动态设置 num_workers
    num_workers = 16

    # Data decoding and augmentation
    image_pipeline = [ToTensor(), ToDevice(device)]
    label_pipeline = [IntDecoder(), ToTensor(), ToDevice(device), Squeeze()]
    # label_pipeline_sample = [ToTensor(), ToDevice(device)]

    # Pipeline for each data field
    pipelines = {
        'image': image_pipeline,
        'label': label_pipeline
    }

    train_dataloader_path = args.train_data_path
    train_dataloader = Loader(train_dataloader_path, batch_size=batch_size, num_workers=num_workers,
                              order=OrderOption.RANDOM, pipelines=pipelines, drop_last=False, seed=0)

    test_dataloader_path = args.test_data_path
    test_dataloader = Loader(test_dataloader_path, batch_size=batch_size, num_workers=num_workers,
                             order=OrderOption.RANDOM, pipelines=pipelines, seed=0)
    test_poison_data = np.load("data/blend/0.1/poisoned_test_data.npz")
    
    test_poison_dataset = TensorDataset(
        torch.tensor(test_poison_data['arr_0'], dtype=torch.float32).permute(0, 3, 1, 2),
        torch.tensor(test_poison_data['arr_1'], dtype=torch.long)
    )
    test_poison_dataloader = DataLoader(test_poison_dataset, batch_size=batch_size, shuffle=False, num_workers=0)
    

    num_classes = 10
    model = VGG16(n_classes=num_classes)  
    # model = nn.DataParallel(model)  # 使用 DataParallel
    model.to(device)
    model.train()

    loss_fn = nn.CrossEntropyLoss()
    optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate, momentum=0.9, weight_decay=5e-4)
    
    # 使用 StepLR 调整学习率，每10个epoch，lr乘0.5
    # scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=10, gamma=0.5)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=5, verbose=True)

    best_accuracy = 0
    best_model = None
    epochs = 100
    MI_inputs_vs_outputs = {class_idx: [] for class_idx in args.observe_classes}
    MI_Y_vs_outputs = {class_idx: [] for class_idx in args.observe_classes}
    SCORES_inputs_vs_outputs = {class_idx: [] for class_idx in args.observe_classes}
    SCORES_outputs_vs_Y = {class_idx: [] for class_idx in args.observe_classes}
    train_accuracies = []
    test_accuracies = []
    train_losses = []
    previous_test_loss = float('inf')
    ASR = []

    for t in range(1, epochs + 1):
        print(f"------------------------------- Epoch {t} -------------------------------")
        train_acc, class_losses = train_loop(train_dataloader, model, loss_fn, optimizer, num_classes)
        test_loss, test_acc = test_loop(test_dataloader, model, loss_fn)
        train_accuracies.append(train_acc)
        test_accuracies.append(test_acc)
        train_losses.append(class_losses)
        _asr = calculate_asr(model, test_poison_dataloader, 0, device)
        ASR.append(_asr)
        # 保存最佳模型
        if test_acc > best_accuracy:
            best_accuracy = test_acc
            best_model = copy.deepcopy(model)
            print(f"New best model saved with accuracy: {best_accuracy:.2f}%")

        # 调整学习率
        scheduler.step(test_loss)
        
        # 检查是否应该计算互信息
        # should_compute_mi = ((t % pow(2, t//10) == 0) or t%10==0) and test_loss < previous_test_loss
        # should_compute_mi = (t % pow(2, t//10) == 0) and (test_loss < previous_test_loss if t < 10 else True)
        # should_compute_mi = test_loss < previous_test_loss
        # should_compute_mi = t==1 or t==5 or t==10 or t==20 or t==40 or t==80
        # should_compute_mi = t==1 or t==8 or t==15 or t==25 or t==40 or t==60
        # should_compute_mi = t==1 or t==8 or t==10 or t==15 or t==20 or t==40 or t==60 or t==80 or t==120
        should_compute_mi = t==1 or t==5 or t==10 or t==20 or t==40 or t==60 or t==100
        # should_compute_mi = False
        if should_compute_mi:
            print(f"------------------------------- Epoch {t} -------------------------------")
            mi_inputs_vs_outputs_dict = {}
            scores_inputs_vs_outputs_dict = {}

            mi_Y_vs_outputs_dict = {}
            scores_outputs_vs_Y_dict = {}

            model_state_dict = model.state_dict()
            # 创建一个进程池
            with concurrent.futures.ProcessPoolExecutor(max_workers=len(args.observe_classes)) as executor:
                # 计算 I(X,T) 和 I(T,Y)
                compute_args = [(args, model_state_dict, 'inputs-vs-outputs', class_idx, 350, mode) 
                                for class_idx in args.observe_classes]
                results_inputs_vs_outputs = list(executor.map(estimate_mi_wrapper, compute_args))

            with concurrent.futures.ProcessPoolExecutor(max_workers=len(args.observe_classes)) as executor:    
                compute_args = [(args, model_state_dict, 'outputs-vs-Y', class_idx, 200, mode) 
                                for class_idx in args.observe_classes]
                results_Y_vs_outputs = list(executor.map(estimate_mi_wrapper, compute_args))

            # 处理结果
            for class_idx, result in zip(args.observe_classes, results_inputs_vs_outputs):
                mi_inputs_vs_outputs, scores_inputs_vs_outputs = result
                mi_inputs_vs_outputs_dict[class_idx] = mi_inputs_vs_outputs
                MI_inputs_vs_outputs[class_idx].append(mi_inputs_vs_outputs)

            for class_idx, result in zip(args.observe_classes, results_Y_vs_outputs):
                mi_Y_vs_outputs, scores_outputs_vs_Y = result
                mi_Y_vs_outputs_dict[class_idx] = mi_Y_vs_outputs
                MI_Y_vs_outputs[class_idx].append(mi_Y_vs_outputs)

            plot_and_save_mi(mi_inputs_vs_outputs_dict, 'inputs-vs-outputs', args.outputs_dir, t)
            plot_and_save_mi(mi_Y_vs_outputs_dict, 'outputs-vs-Y', args.outputs_dir, t)
            # analyze_sample_scores(scores_inputs_vs_outputs_dict, 'inputs-vs-outputs', args.outputs_dir, t)
            # analyze_sample_scores(scores_outputs_vs_Y_dict, 'outputs-vs-Y', args.outputs_dir, t)
        
        # 更新前一个epoch的test_loss
        previous_test_loss = test_loss
        # if t % 10 == 0:
        #     torch.save(model, os.path.join(args.outputs_dir, f'model_epoch_{t}.pth'))
    # np.save(os.path.join(args.outputs_dir, 'train_losses_by_class.npy'), np.array(train_losses))
    # torch.save(model, os.path.join(args.outputs_dir, 'model_80.pth'))
    plot_train_acc_ASR(train_accuracies, test_accuracies, ASR, epochs, args.outputs_dir)
    plot_train_loss_by_class(train_losses, epochs, num_classes, args.outputs_dir)
    # 训练完成后
    print("Computing class-wise accuracies...")
    if not best_model:
        best_model = model
    train_accuracies = compute_class_accuracy(best_model, train_dataloader, num_classes)
    test_accuracies = compute_class_accuracy(best_model, test_dataloader, num_classes)

    print("Train accuracies per class:")
    for i, acc in enumerate(train_accuracies):
        print(f"Class {i}: {acc:.2f}%")

    print("\nTest accuracies per class:")
    for i, acc in enumerate(test_accuracies):
        print(f"Class {i}: {acc:.2f}%")

    return MI_inputs_vs_outputs, MI_Y_vs_outputs, best_model, train_accuracies, test_accuracies

def analyze_sample_scores(sample_scores_dict, flag, output_dir, epoch):
    # 将所有类别的得分合并到一个列表中
    all_scores = []
    all_class_indices = []
    all_sample_indices = []
    
    for class_idx, scores in sample_scores_dict.items():
        all_scores.append(scores.cpu())  # 确保scores在CPU上
        all_class_indices.extend([class_idx] * len(scores))
        all_sample_indices.append(torch.arange(len(scores)))
    
    all_scores = torch.cat(all_scores)
    all_class_indices = torch.tensor(all_class_indices)
    all_sample_indices = torch.cat(all_sample_indices)
    
    # 计算所有得分的统计信息
    mean_score = all_scores.mean().item()
    std_score = all_scores.std().item()
    
    # 找出得分异常高的样本（例如，高于平均值两个标准差）
    threshold = mean_score + 2 * std_score
    suspicious_mask = all_scores > threshold
    suspicious_scores = all_scores[suspicious_mask]
    suspicious_classes = all_class_indices[suspicious_mask]
    suspicious_indices = all_sample_indices[suspicious_mask]
    
    print(f"Overall - Mean score: {mean_score:.4f}, Std: {std_score:.4f}")
    print(f"Number of suspicious samples: {len(suspicious_scores)}")
    
    # 保存可疑样本的信息
    suspicious_info = np.column_stack((suspicious_classes.cpu().detach().numpy(), 
                                       suspicious_indices.cpu().detach().numpy(), 
                                       suspicious_scores.cpu().detach().numpy()))
    np.save(os.path.join(output_dir, f'suspicious_samples_by_{flag}_epoch_{epoch}.npy'), suspicious_info)
    
    # 绘制每个类别的得分箱线图
    plt.figure(figsize=(12, 6))
    plt.boxplot([scores.cpu().detach().numpy() for scores in sample_scores_dict.values()], labels=sample_scores_dict.keys())
    plt.title(f'Score Distribution by {flag} - Epoch {epoch}')
    plt.xlabel('Class')
    plt.ylabel('Score')
    plt.savefig(os.path.join(output_dir, f'score_distribution_by_{flag}_epoch_{epoch}.png'))
    plt.close()
    
    # 输出每个类别中可疑样本的数量
    for class_idx in sample_scores_dict.keys():
        class_suspicious_count = (suspicious_classes == class_idx).sum().item()
        print(f"Class {class_idx} - Number of suspicious samples: {class_suspicious_count}")

    return suspicious_info

def ob_infoNCE(args):
    outputs_dir = args.outputs_dir
    if not os.path.exists(outputs_dir):
        os.makedirs(outputs_dir)
    infoNCE_MI_log_inputs_vs_outputs, infoNCE_MI_log_Y_vs_outputs, best_model, train_accuracies, test_accuracies = train(args, 'inputs-vs-outputs', 'infoNCE')
     
    # 保存最佳模型
    # torch.save(best_model, os.path.join(args.outputs_dir, 'best_model.pth'))

    # 检查并保存 infoNCE_MI_log_inputs_vs_outputs
    infoNCE_MI_log_inputs_vs_outputs = np.array(infoNCE_MI_log_inputs_vs_outputs, dtype=object)
    np.save(f'{outputs_dir}/infoNCE_MI_I(X,T).npy', infoNCE_MI_log_inputs_vs_outputs)
    print(f'saved in {outputs_dir}/infoNCE_MI_I(X,T).npy')
    
    # 检查并保存 infoNCE_MI_log_Y_vs_outputs
    infoNCE_MI_log_Y_vs_outputs = np.array(infoNCE_MI_log_Y_vs_outputs, dtype=object)
    np.save(f'{outputs_dir}/infoNCE_MI_I(Y,T).npy', infoNCE_MI_log_Y_vs_outputs)
    print(f'saved in {outputs_dir}/infoNCE_MI_I(Y,T).npy')

if __name__ == '__main__':
    device = torch.device('cuda')
    mp.set_start_method('spawn', force=True)
    torch.manual_seed(0)
    parser = argparse.ArgumentParser()
    parser.add_argument('--outputs_dir', type=str, default='results/ob_infoNCE_06_22', help='output_dir')
    parser.add_argument('--sampling_datasize', type=str, default='1000', help='sampling_datasize')
    parser.add_argument('--training_epochs', type=str, default='100', help='training_epochs')
    parser.add_argument('--batch_size', type=str, default='256', help='batch_size')
    parser.add_argument('--learning_rate', type=str, default='1e-5', help='learning_rate')
    parser.add_argument('--mi_estimate_epochs', type=str, default='300', help='mi_estimate_epochs')
    parser.add_argument('--mi_estimate_lr', type=str, default='1e-6', help='mi_estimate_lr')
    parser.add_argument('--class', type=str, default='0', help='class')
    parser.add_argument('--train_data_path', type=str, default='0', help='class')
    parser.add_argument('--test_data_path', type=str, default='0', help='class')
    parser.add_argument('--sample_data_path', type=str, default='data/badnet/0.1/train_data', help='class')
    # parser.add_argument('--observe_classes', type=list, default=[0,1,2,3,4,5,6,7,8,9], help='class')
    parser.add_argument('--observe_classes', type=list, default=[0,1,2,3,4,5,6,7,8,9,'0_sample','0_backdoor','0_clean'], help='class')
    args = parser.parse_args()
    # ob_DV()
    ob_infoNCE(args)