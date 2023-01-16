# Copyright (c) Meta Platforms, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass

import magnum as mn
import numpy as np
import os
from os import path as osp

from fairmotion.ops import motion as motion_ops

from habitat.utils.fairmotion_utils import (
    MotionData,
    AmassHelper
)

import pybullet as p


class Motions:
    """
    The Motions class is collection of stats that will hold the different movement motions
    for the character to use when following a path. The character is left-footed so that
    is our reference for with step the motions assume first.
    """
    def __init__(self, amass_path, body_model_path):
        self.amass_path = amass_path
        self.body_model_path = body_model_path

        # logger.info("Loading Motion data...")

        # TODO: add more diversity here
        motion_files = {
            "walk": f"{self.amass_path}/CMU/10/10_04_poses.npz",  # [0] cycle walk
            "run": f"{self.amass_path}/CMU/09/09_01_poses.npz",  # [1] cycle run
        }

        motion_data = {key: AmassHelper.load_amass_file(value, bm_path=body_model_path) for key, value in motion_files.items()}

        ### TRANSITIVE ###
        # all motions must have same fps for this implementation, so use first motion to set global
        fps = motion_data['walk'].fps

        # Standing pose that must be converted (amass -> habitat joint positions)
        self.standing_pose = motion_data['walk'].poses[0]

        # Walk-to-walk cycle
        self.walk_to_walk = MotionData(
            motion_ops.cut(motion_data['walk'], 300, 430)
        )

        # Run-to-run cycle
        self.run_to_run = MotionData(motion_ops.cut(motion_data['run'], 3, 89))



class AmassHumanController:
    def __init__(self, urdf_path, amass_path, body_model_path, grab_path=None, obj_translation=None, draw_fps=60):
        self.motions = Motions(amass_path, body_model_path)

        self.last_pose = self.motions.standing_pose
        self.urdf_path = urdf_path
        self.amass_path = amass_path

        self.ROOT = 0

        self.mocap_frame = 0
        self.curr_trans = mn.Vector3([0,0,0.3])
        self.rotation_offset: Optional[mn.Quaternion] = mn.Quaternion()
        self.translation_offset: Optional[mn.Vector3] = mn.Vector3([0, 0, 0])
        self.obj_transform = self.obtain_root_transform_at_frame(0)

        self.prev_orientation = None

        # smoothing_params
        self.frames_to_stop = 10
        self.frames_to_start = 10
        self.draw_fps = draw_fps

        # state variables
        self.time_since_stop = 0 # How many frames since we started stopping
        self.time_since_start = 0 # How many frames since we started walking
        self.fully_started = False
        self.fully_stopped = False
        self.last_walk_pose = None


        self.path_ind = 0
        self.path_distance_covered_next_wp = 0 # The distance we will have to cover in the next WP
        self.path_distance_walked = 0

        # Option args
        self.use_ik_grab = True

        if obj_translation is not None:
            self.translation_offset = obj_translation + mn.Vector3([0,0.90,0])
        self.pc_id = p.connect(p.DIRECT)
        self.human_bullet_id = p.loadURDF(urdf_path)

        self.link_ids = list(range(p.getNumJoints(self.human_bullet_id)))

        # TODO: There is a mismatch between the indices we get here and ht eones from model.get_link_ids. Would be good to resolve it
        link_indices = [0, 1, 2, 6, 7, 8, 14, 11, 12, 13, 9, 10, 15, 16, 17, 18, 3, 4, 5]

        # Joint INFO
        # https://github.com/bulletphysics/bullet3/blob/master/docs/pybullet_quickstart_guide/PyBulletQuickstartGuide.md.html#getjointinfo

        self.joint_info = [p.getJointInfo(self.human_bullet_id, index) for index in link_indices]

        # Data used to grab
        # self.use_ik_grab = False
        if grab_path is None or len(grab_path) == 0:
            self.use_ik_grab = False
        if self.use_ik_grab:
            graph_path_split = osp.splitext(grab_path)
            grab_poses = "{}{}.{}".format(graph_path_split[0], '_processed_', graph_path_split[1])
            grab_data = np.load(grab_path)
            num_poses = grab_data['trans'].shape[0]
            self.num_pos = num_poses

            # TODO: change this literal argument
            if not os.path.isfile(grab_poses):
                self.grab_quaternions = np.zeros((num_poses, 68))
                self.grab_transform = np.zeros((num_poses, 16))
                from tqdm import tqdm
                for pose_index in tqdm(range(num_poses)):
                    current_pose = MotionData.obtain_pose(self.last_pose.skel, grab_data, pose_index)
                    pose_quaternion, root_trans, root_rot = AmassHelper.convert_CMUamass_single_pose(current_pose, self.joint_info, raw=True)
                    transform_as_mat = np.array(mn.Matrix4.from_(root_rot.to_matrix(), root_trans)).reshape(-1)
                    self.grab_quaternions[pose_index, :] = pose_quaternion
                    self.grab_transform[pose_index, :] = transform_as_mat
                np.savez(grab_poses, joints=self.grab_quaternions, root_transform=self.grab_transform)
            else:
                pick = np.load(grab_poses)
                self.grab_quaternions = pick['joints']
                self.grab_transform = pick['root_transform']
            self.coords_grab = grab_data['coord']
            # breakpoint()
            self.vpose = {
                'min': self.coords_grab.min(0),
                'max': self.coords_grab.max(0),
                'bins': [40, 40, 10]
            }
        self.reach_pos = 0
        # breakpoint()

    def reset(self, position) -> None:
        """Reset the joints on the human. (Put in rest state)
        """
        self.translation_offset = position
        self.last_pose = self.motions.standing_pose

    def stop(self, progress=None):

        new_pose =  self.motions.standing_pose
        new_pose, new_root_trans, root_rotation = AmassHelper.convert_CMUamass_single_pose(new_pose, self.joint_info)

        if progress is None:
            self.time_since_stop += 1
            if self.fully_started:
                progress = min(self.time_since_stop, self.frames_to_stop)
            else:
                # If we were just starting walking and are stopping again, the progress is
                # just reduced by the steps we started to walk
                progress = max(0, self.frames_to_stop - self.time_since_start)
            progress =  progress * 1.0/self.frames_to_stop
        else:
            self.time_since_stop = int(progress * self.frames_to_stop)

        if progress == 1:
            self.fully_stopped = True
        else:
            self.fully_stopped = False

        if self.last_walk_pose is not None:
            # If we walked in the past, interpolate between walk and stop
            interp_pose =  np.array(self.last_walk_pose) * (1-progress) + np.array(new_pose) * progress
            interp_pose = list(interp_pose)
        else:
            interp_pose = new_pose

        self.time_since_start = 0

        return interp_pose, self.obj_transform





    def walk_path(self, path_keypoints: List[mn.Vector3], should_reset=False, stop_at_end=False):
        """ Walks along a path """
        if should_reset:
            self.reset_path_info()


        while self.path_ind < len(path_keypoints) - 1 and self.path_distance_walked > self.path_distance_covered_next_wp:
            i = self.path_ind
            j = i + 1
            self.path_ind += 1

            progress: float = (mn.Vector3(path_keypoints[i] - path_keypoints[j])).length()
            self.path_distance_covered_next_wp += progress

        is_last_keypoint = self.path_ind == (len(path_keypoints) - 1)
        next_relative_position = path_keypoints[self.path_ind] - self.translation_offset
        new_pos, new_transform = self.walk(next_relative_position)

        if is_last_keypoint and stop_at_end:
            # Set a stopping pose if close to the end
            distance_to_goal = (path_keypoints[self.path_ind] - self.translation_offset).length()
            progress_stop = min(0, self.stop_distance - distance_to_goal) / self.stop_distance
            new_pos = self.stop(progress=progress_stop)

        return new_pos, new_transform

    def obtain_root_transform_at_frame(self, mocap_frame):

        curr_motion_data = self.motions.walk_to_walk
        global_neutral_correction = AmassHelper.global_correction_quat(
            mn.Vector3.z_axis(), curr_motion_data.direction_forward
        )
        full_transform = curr_motion_data.motion.poses[mocap_frame].get_transform(
            self.ROOT, local=True
        )

        full_transform = mn.Matrix4(full_transform)
        full_transform.translation -= curr_motion_data.center_of_root_drift
        full_transform = (
            mn.Matrix4.from_(
                global_neutral_correction.to_matrix(), mn.Vector3()
            )
            @ full_transform
        )
        return full_transform

    @property
    def step_distance(self):
        step_size = int(self.motions.walk_to_walk.fps / self.draw_fps)
        curr_motion_data = self.motions.walk_to_walk

        prev_distance = curr_motion_data.map_of_total_displacement[self.mocap_frame]
        new_pos = self.mocap_frame + step_size
        if new_pos < len(curr_motion_data.map_of_total_displacement):
            distance_covered = curr_motion_data.map_of_total_displacement[new_pos]
        else:
            pos_norm = new_pos % len(curr_motion_data.map_of_total_displacement)
            distance_covered = curr_motion_data.map_of_total_displacement[-1]
            distance_covered +=  max(0, (step_size // len(curr_motion_data.map_of_total_displacement)) - 1)
            distance_covered += curr_motion_data.map_of_total_displacement[pos_norm];
        return distance_covered - prev_distance

    def _select_index(self, position: mn.Vector3):
        def find_index_quant(minv, maxv, num_bins, value):
            # Find the quantization bin
            value = max(min(value, maxv), minv)
            value_norm = (value - minv) / (maxv - minv)
            # TODO: make sure that this is not round
            index = int(value_norm * num_bins)
            return min(index, num_bins - 1)

        relative_pos = position
        x_diff, y_diff, z_diff = relative_pos.x, relative_pos.y, relative_pos.z
        # breakpoint()
        coord_data = [
            (self.vpose['min'][0], self.vpose['max'][0], self.vpose['bins'][0], x_diff),
            (self.vpose['min'][1], self.vpose['max'][1], self.vpose['bins'][1], y_diff),
            (self.vpose['min'][2], self.vpose['max'][2], self.vpose['bins'][2], z_diff),
        ]
        # print(x_diff, y_diff, z_diff)
        # breakpoint()
        x_ind, y_ind, z_ind = [find_index_quant(*data) for data in coord_data]
        # print(x_ind, y_ind, z_ind)
        index = y_ind * self.vpose['bins'][0] * self.vpose['bins'][2] + x_ind * self.vpose['bins'][2] + z_ind
        # print(self.coords_grab[index])
        # breakpoint()
        return index

    def reach(self, position: mn.Vector3):
        # Move the X hand towards that position

        # Make position relative to the root
        # position_relative = position - self.root_pos


        if not self.use_ik_grab:
            raise KeyError(
                "Error: reach behavior is not defined when use_ik_grab is off"
            )
        reach_pos = self._select_index(position)
        curr_pose = list(self.grab_quaternions[reach_pos])
        curr_transform = mn.Matrix4(self.grab_transform[reach_pos].reshape(4,4))
        curr_transform.translation = self.obj_transform.translation
        # breakpoint()
        return curr_pose, curr_transform

    def walk(self, position: mn.Vector3):
        """ Walks to the desired position. Rotates the character if facing in a different direction """
        step_size = int(self.motions.walk_to_walk.fps / self.draw_fps)
        # breakpoint()
        self.mocap_frame = (self.mocap_frame +  step_size) % self.motions.walk_to_walk.motion.num_frames()
        if self.mocap_frame == 0:
            self.distance_rot = 0
        # curr_pos = self.motions.walk_to_walk[self.mocap_frame]
        new_pose = self.motions.walk_to_walk.poses[self.mocap_frame]
        curr_motion_data = self.motions.walk_to_walk
        new_pose, new_root_trans, root_rotation = AmassHelper.convert_CMUamass_single_pose(new_pose, self.joint_info)

        char_pos = self.translation_offset



        forward_V = position


        # interpolate facing last margin dist with standing pose
        did_rotate = False
        if self.prev_orientation is not None:
            action_order_facing = self.prev_orientation
            curr_angle = np.arctan2(forward_V[0], forward_V[2]) * 180./np.pi
            prev_angle = np.arctan2(action_order_facing[0], action_order_facing[2]) * 180./np.pi
            forward_angle = curr_angle - prev_angle
            if np.abs(forward_angle) > 1:
                # t = forward_angle
                actual_angle_move = 5
                if abs(forward_angle) < actual_angle_move:
                    actual_angle_move = abs(forward_angle)
                new_angle = (prev_angle + actual_angle_move * np.sign(forward_angle)) * np.pi / 180
                did_rotate = True
            else:
                new_angle = curr_angle * np.pi / 180


            forward_V = mn.Vector3(np.sin(new_angle), 0, np.cos(new_angle))



        forward_V[1] = 0.
        forward_V = mn.Vector3(forward_V)
        forward_V = forward_V.normalized()

        look_at_path_T = mn.Matrix4.look_at(
                    char_pos, char_pos + forward_V.normalized(), mn.Vector3.y_axis()
                )

        full_transform = self.obtain_root_transform_at_frame(self.mocap_frame)
        # while transform is facing -Z, remove forward displacement
        full_transform.translation *= mn.Vector3.x_axis() + mn.Vector3.y_axis()
        full_transform = look_at_path_T @ full_transform

        prev_distance = curr_motion_data.map_of_total_displacement[self.mocap_frame - step_size]
        if (self.mocap_frame - step_size) < 0:
            distance_covered = curr_motion_data.map_of_total_displacement[self.mocap_frame] + curr_motion_data.map_of_total_displacement[-1]
        else:
            distance_covered = curr_motion_data.map_of_total_displacement[self.mocap_frame];

        dist_diff = max(0, distance_covered - prev_distance)
        self.translation_offset = self.translation_offset + forward_V * dist_diff;
        self.prev_orientation = forward_V


        self.time_since_start += 1
        if self.fully_stopped:
            progress = min(self.time_since_start, self.frames_to_start)
        else:
            # if it didn't fully stop it should take us to walk as many
            # frames as the time we spent stopping
            progress = max(0, self.frames_to_start - self.time_since_stop)

        # Ensure a smooth transition from walking to standing
        progress_norm = progress * 1.0/self.frames_to_start
        if progress_norm < 1.0:
            # if it was standing before walking, interpolate between walking and standing pose
            standing_pose, _, _ = AmassHelper.convert_CMUamass_single_pose(self.motions.standing_pose, self.joint_info)
            interp_pose = (1-progress_norm) * np.array(standing_pose) + progress_norm * np.array(new_pose)
            interp_pose = list(interp_pose)
            self.fully_started = False
        else:
            interp_pose = new_pose
            self.fully_started = True

        if self.time_since_start >= self.frames_to_start:
            self.fully_started = True
        self.time_since_stop = 0
        self.last_walk_pose = new_pose

        self.obj_transform = full_transform
        return interp_pose, full_transform


    @classmethod
    def transformAction(cls, pose: List, transform: mn.Matrix4):
        return pose + list(np.asarray(transform.transposed()).flatten())


    def open_gripper(self):
        pass
