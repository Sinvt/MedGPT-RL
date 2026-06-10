import os
from tensorboard.backend.event_processing import event_accumulator

def extract_logs(log_dir):
    if not os.path.exists(log_dir):
        return {}
        
    runs = [os.path.join(log_dir, d) for d in os.listdir(log_dir) if os.path.isdir(os.path.join(log_dir, d))]
    
    results = {}
    for run in runs:
        # Find tfevents file
        events_files = [f for f in os.listdir(run) if 'events.out.tfevents' in f]
        if not events_files:
            continue
            
        ea = event_accumulator.EventAccumulator(os.path.join(run, events_files[0]))
        ea.Reload()
        
        tags = ea.Tags()['scalars']
        run_results = {}
        for tag in tags:
            events = ea.Scalars(tag)
            # Just grab the min, max, first, and last values for analysis
            values = [e.value for e in events]
            steps = [e.step for e in events]
            
            run_results[tag] = {
                'first': values[0],
                'last': values[-1],
                'min': min(values),
                'max': max(values),
                'steps': len(values),
                'final_step': steps[-1]
            }
        results[os.path.basename(run)] = run_results
    return results

print("=== SFT Logs ===")
sft_logs = extract_logs("outputs-sft-qwen-0.5b/runs")
for run, tags in sft_logs.items():
    if 'train/loss' in tags:
        loss = tags['train/loss']
        print(f"SFT train/loss: First={loss['first']:.4f}, Last={loss['last']:.4f}, Min={loss['min']:.4f}")
    if 'eval/loss' in tags:
        loss = tags['eval/loss']
        print(f"SFT eval/loss: First={loss['first']:.4f}, Last={loss['last']:.4f}, Min={loss['min']:.4f}")

print("\n=== RM Logs ===")
rm_logs = extract_logs("outputs-rm-qwen-0.5b/runs")
for run, tags in rm_logs.items():
    if 'train/loss' in tags:
        loss = tags['train/loss']
        print(f"RM train/loss: First={loss['first']:.4f}, Last={loss['last']:.4f}, Min={loss['min']:.4f}")
    if 'eval/loss' in tags:
        loss = tags['eval/loss']
        print(f"RM eval/loss: First={loss['first']:.4f}, Last={loss['last']:.4f}")
    if 'eval/accuracy' in tags:
        acc = tags['eval/accuracy']
        print(f"RM eval/accuracy: First={acc['first']:.4f}, Last={acc['last']:.4f}, Max={acc['max']:.4f}")

print("\n=== PPO Logs ===")
ppo_logs = extract_logs("outputs-ppo-qwen-0.5b/runs")
for run, tags in ppo_logs.items():
    for tag_name in ['train/loss', 'train/reward', 'train/kl', 'eval/reward', 'eval/kl', 'train/rewards/merged-rm-qwen-0.5b/mean']:
        if tag_name in tags:
            metric = tags[tag_name]
            print(f"PPO {tag_name}: First={metric['first']:.4f}, Last={metric['last']:.4f}, Min={metric['min']:.4f}, Max={metric['max']:.4f}")

