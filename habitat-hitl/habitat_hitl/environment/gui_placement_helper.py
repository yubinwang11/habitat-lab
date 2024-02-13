#!/usr/bin/env python3

# Copyright (c) Meta Platforms, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math
from typing import Final

import magnum as mn

from habitat_sim.physics import CollisionGroups

COLOR_PLACE_PREVIEW_VALID: Final[mn.Color3] = mn.Color3(1, 1, 1)
COLOR_PLACE_PREVIEW_INVALID: Final[mn.Color3] = mn.Color3(1, 0, 0)
RADIUS_PLACE_PREVIEW_VALID = 0.25
RADIUS_PLACE_PREVIEW_INVALID = 0.05

FAR_AWAY_HIDDEN_POSITION = mn.Vector3(0, -1000, 0)
DEFAULT_GRAVITY = mn.Vector3(0, -1, 0)


class GuiPlacementHelper:
    """Helper for placing objects from the GUI."""

    def __init__(self, gui_service, gravity_dir=DEFAULT_GRAVITY):
        self._app_service = gui_service
        self._gravity_dir = gravity_dir

    def _find_snap_pos(self, ray, query_obj):
        sim = self._app_service.sim

        assert query_obj.collidable

        # move object far away so it doesn't interfere with raycast
        query_obj.translation = FAR_AWAY_HIDDEN_POSITION

        raycast_results = sim.cast_ray(ray=ray)
        if not raycast_results.has_hits():
            return False, None

        hit_info = raycast_results.hits[0]

        hit_pos = hit_info.point

        max_placement_dist = 2.5
        if hit_info.ray_distance > max_placement_dist:
            return False, hit_pos

        hit_normal = hit_info.normal

        adjusted_hit_pos = mn.Vector3(hit_pos)

        # search away from hit surface for free place
        search_away_dist = 0.5  # this should be >= max object radius
        search_inc_dist = 0.03
        search_inc_offset = hit_normal * search_inc_dist
        num_incs = int(math.ceil(search_away_dist / search_inc_dist))
        success = False
        for _ in range(num_incs):
            query_obj.translation = adjusted_hit_pos
            if not query_obj.contact_test():
                success = True
                break
            adjusted_hit_pos += search_inc_offset

        if not success:
            return False, hit_pos

        # search down until non-free
        search_down_dist = 0.1
        search_inc_dist = 0.015
        search_inc_offset = self._gravity_dir * search_inc_dist
        num_incs = int(math.ceil(search_down_dist / search_inc_dist))
        success = False
        for _ in range(num_incs):
            adjusted_hit_pos += search_inc_offset
            query_obj.translation = adjusted_hit_pos
            if query_obj.contact_test():
                success = True
                break

        if not success:
            return False, hit_pos

        return True, adjusted_hit_pos

    def update(self, ray, query_obj_id):
        sim = self._app_service.sim
        query_obj = sim.get_rigid_object_manager().get_object_by_id(
            query_obj_id
        )

        cached_is_collidable = query_obj.collidable
        query_obj.collidable = True

        # sloppy: change the collision group so that contact_test will work. We should restore the original collision group after this query, but we can't because we don't have a get_collision_group API.
        query_obj.override_collision_group(CollisionGroups.Default)

        success, hint_pos = self._find_snap_pos(ray, query_obj)
        query_obj.collidable = cached_is_collidable

        if success:
            self._draw_circle(
                hint_pos, COLOR_PLACE_PREVIEW_VALID, RADIUS_PLACE_PREVIEW_VALID
            )
        else:
            query_obj.translation = FAR_AWAY_HIDDEN_POSITION
            self._draw_circle(
                hint_pos,
                COLOR_PLACE_PREVIEW_INVALID,
                RADIUS_PLACE_PREVIEW_INVALID,
            )

        return hint_pos if success else None

    def _draw_circle(self, pos, color, radius):
        num_segments = 24
        self._app_service.line_render.draw_circle(
            pos,
            radius,
            color,
            num_segments,
        )
        if self._app_service.client_message_manager:
            self._app_service.client_message_manager.add_highlight(pos, radius)
