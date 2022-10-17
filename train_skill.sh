#!/bin/bash
## SLURM scripts have a specific format. 

#SBATCH --job-name=pick_depth_scaled_no_base_mov
#SBATCH --output=../rearrange_trains/pick_depth_scaled_no_base_mov/slurm.out
#SBATCH --error=../rearrange_trains/pick_depth_scaled_no_base_mov/slurm.err
#SBATCH --partition=learnfair
#SBATCH --nodes=8
#SBATCH --gpus-per-node=8
#SBATCH --ntasks-per-node=4
#SBATCH --gpus-per-task=2
#SBATCH --cpus-per-task=17
#SBATCH --open-mode=append
#SBATCH --time=24:00:00
#SBATCH --signal=USR1@60

# setup conda and shell environments
module purge
module load anaconda3
eval "$(conda shell.bash hook)"
conda activate habitat

# Setup slurm multinode
export GLOG_minloglevel=2
export MAGNUM_LOG=quiet
MAIN_ADDR=$(scontrol show hostnames "${SLURM_JOB_NODELIST}" | head -n 1)
export MAIN_ADDR
set -x

echo 1
# Run training
srun python -u habitat_baselines/run.py --exp-config habitat_baselines/config/rearrange/ddppo_pick.yaml --run-type train
#srun python -u habitat_baselines/run.py --exp-config habitat_baselines/config/rearrange/ddppo_place.yaml --run-type train
#srun python -u habitat_baselines/run.py --exp-config habitat_baselines/config/rearrange/ddppo_nav_to_obj.yaml --run-type train

#srun python -u habitat-lab/habitat_baselines/run.py --exp-config habitat-challenge/configs/methods/tp_t.yaml --run-type train TASK_CONFIG.TASK.TASK_SPEC_BASE_PATH habitat-challenge/configs/pddl/