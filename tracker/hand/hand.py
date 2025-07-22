import cv2
from mediapipe import solutions
from mediapipe.framework.formats import landmark_pb2
import numpy as np
import mediapipe as mp
from scipy.spatial.transform import Rotation as R
from copy import deepcopy
import utils.globals as g
import joblib
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import PolynomialFeatures
# from collections import deque
# import threading, queue

def draw_hand_landmarks(rgb_image):
    MARGIN = 10  # pixels
    FONT_SIZE = 1
    FONT_THICKNESS = 1
    HANDEDNESS_TEXT_COLOR = (88, 205, 54)  # vibrant green

    hand_landmarks_list = g.hand_landmarks
    handedness_list = g.handedness

    if hand_landmarks_list is None or handedness_list is None:
        return rgb_image

    # Loop through each detected hand using zip
    for idx, (hand, hand_landmarks) in enumerate(zip(g.handedness, g.hand_landmarks)):
        # Draw the hand landmarks.
        hand_landmarks_proto = landmark_pb2.NormalizedLandmarkList()
        hand_landmarks_proto.landmark.extend(
            [
                landmark_pb2.NormalizedLandmark(x=landmark.x, y=landmark.y, z=landmark.z)
                for landmark in hand_landmarks.landmark  # Access through .landmark
            ]
        )
        solutions.drawing_utils.draw_landmarks(
            rgb_image,
            hand_landmarks_proto,
            solutions.hands.HAND_CONNECTIONS,
            solutions.drawing_styles.get_default_hand_landmarks_style(),
            solutions.drawing_styles.get_default_hand_connections_style(),
        )

        # Get the top left corner of the detected hand's bounding box.
        height, width, _ = rgb_image.shape
        x_coordinates = [landmark.x for landmark in hand_landmarks.landmark]
        y_coordinates = [landmark.y for landmark in hand_landmarks.landmark]
        text_x = int(min(x_coordinates) * width)
        text_y = int(min(y_coordinates) * height) - MARGIN

        # Draw handedness (left or right hand) on the image.
        cv2.putText(
            rgb_image,
            f"{'Right' if hand.classification[0].label == 'Left' else 'Left'}",  # Handedness label
            (text_x, text_y),
            cv2.FONT_HERSHEY_DUPLEX,
            FONT_SIZE,
            HANDEDNESS_TEXT_COLOR,
            FONT_THICKNESS,
            cv2.LINE_AA,
        )

    return rgb_image


def get_hand_pose(landmarks, reverse_flag=True):
    hand_pose = np.asarray([[l.x, l.y, l.z] for l in landmarks])
    if reverse_flag:
        hand_pose[:, 0] = -hand_pose[:, 0]  # flip the points a bit since steamvrs coordinate system is a bit diffrent
        hand_pose[:, 1] = -hand_pose[:, 1]
    return hand_pose

def calc_angle(vec1, vec2):
    with np.errstate(divide='ignore', invalid='ignore'):
        cross = np.linalg.norm(np.cross(vec1, vec2))
        dot = np.dot(vec1, vec2)
        return np.degrees(np.abs(np.arctan2(cross, dot)))

def finger_handling(hand_pose):
    global finger_mapper
    finger_curl= {}
    finger_names = ["thumb", "index", "middle", "ring", "pinky"]
    for name in finger_names:
        cfg = g.config["Tracking"]["Finger"]
        base_start, base_end = cfg[f"{name}_base"]
        tip_start, tip_end = cfg[f"{name}_tip"]
        min_val = cfg[f"{name}_min"]
        max_val = cfg[f"{name}_max"]
        base_vec = hand_pose[base_end] - hand_pose[base_start]
        tip_vec = hand_pose[tip_end] - hand_pose[tip_start]
        if np.linalg.norm(base_vec) < 1e-6 or np.linalg.norm(tip_vec) < 1e-6:
            raw_angle = 0.0
        else:
            raw_angle = calc_angle(base_vec, tip_vec)
        clamped = np.clip(raw_angle, min_val, max_val)
        range_val = max_val - min_val
        norm_value = 1 - (clamped - min_val) / range_val
        norm_value = np.clip(norm_value, 0.1, 1.0)
        finger_curl[name] = round(norm_value,1)
        # finger_curl[name] = finger_mapper[name]["mapper"](round(norm_value,1))

    return finger_curl

def compute_bounding_size(reference_kp):
    xs = [kp.x for kp in reference_kp]
    ys = [kp.y for kp in reference_kp]
    return max(xs)-min(xs), max(ys)-min(ys)

def calculate_normalized_distance(kp1, kp2, reference_kp, points):
    distances = [
        np.linalg.norm(np.array([kp1[i].x, kp1[i].y]) - np.array([kp2[i].x, kp2[i].y]))
        for i in points
    ]
    avg_distance = np.mean(distances)
    width, height = compute_bounding_size(reference_kp)
    return avg_distance / max(width, height)


prev_hands = {}  # {key: {'left': landmarks, 'right': landmarks}}
def hand_is_changed(key, hand_name, hand_landmarks, change_points, change_threshold, update_flag=True):
    global prev_hands
    all_keypoints = [hand_landmarks.landmark[i] for i in range(21)]
    reference_keypoints = [hand_landmarks.landmark[i] for i in [0, 1, 17]]
    other_hand = 'Right' if hand_name == 'Left' else 'Left'
    if key not in prev_hands:
        prev_hands[key] = {}
    swap_flag = False
    if prev_hands[key]:
        if hand_name in prev_hands[key] and other_hand in prev_hands[key]:
            self_dist = calculate_normalized_distance(
                all_keypoints,
                prev_hands[key][hand_name],
                reference_keypoints,
                change_points
            )
            other_dist = calculate_normalized_distance(
                all_keypoints,
                prev_hands[key][other_hand],
                reference_keypoints,
                change_points
            )
            swap_flag = other_dist < self_dist and (self_dist - other_dist) > g.config["Tracking"]["Hand"]["hand_swap_threshold"]

    changed = False
    norm_distance = 0
    if hand_name in prev_hands[key]:
        norm_distance = calculate_normalized_distance(
            all_keypoints,
            prev_hands[key][hand_name],
            reference_keypoints,
            change_points
        )
        changed = norm_distance >= change_threshold
    else:
        changed = True
    if update_flag and not swap_flag:
        prev_hands[key][hand_name] = all_keypoints
    return changed, norm_distance, swap_flag


hand_detection_counts = {"Left":0,"Right":0}
finger_action_threshold = {"Left":0,"Right":0}
prev_distance_scalar = None
def hand_pred_handling(detection_result):
    global hand_detection_counts, finger_action_threshold,prev_distance_scalar

    g.hand_landmarks = detection_result.multi_hand_landmarks
    g.handedness = detection_result.multi_handedness

    hand_detection_counts["Left"] -= 1
    if hand_detection_counts["Left"] < 0:
        hand_detection_counts["Left"] = 0
    hand_detection_counts["Right"] -= 1
    if hand_detection_counts["Right"] < 0:
        hand_detection_counts["Right"] = 0

    if detection_result.multi_hand_landmarks is not None and detection_result.multi_handedness is not None and detection_result.multi_hand_world_landmarks is not None:
        same_hand_flag=None
        if len(detection_result.multi_handedness)==2:
            hands=detection_result.multi_handedness
            if hands[0].classification[0].label == hands[1].classification[0].label:
                hand_name = "Right" if hands[0].classification[0].label == "Left" else "Left"
                _,avg_distance_0,_ = hand_is_changed("hand_change", hand_name, detection_result.multi_hand_landmarks[0],
                                                       [0,1,17],
                                                       0, False)
                _,avg_distance_1,_ = hand_is_changed("hand_change", hand_name, detection_result.multi_hand_landmarks[1],
                                                       [0,1,17],
                                                       0, False)
                if avg_distance_0<=avg_distance_1:
                    same_hand_flag=1
                else:
                    same_hand_flag=0

        for idx, (hand, hand_landmarks, hand_world_landmarks) in enumerate(
                zip(detection_result.multi_handedness, detection_result.multi_hand_landmarks,
                    detection_result.multi_hand_world_landmarks)):
            hand_name = "Right" if hand.classification[0].label == "Left" else "Left"
            if same_hand_flag == idx:
                continue
            else:
                _,_,_ = hand_is_changed("hand_change", hand_name, hand_landmarks,
                                                       [0,1,17],
                                                       0)
            if hand.classification[0].score < g.config["Tracking"]["Hand"]["hand_confidence"] or (g.config["Tracking"]["LeftController"]["enable"] and hand_name=="Left") or (g.config["Tracking"]["RightController"]["enable"] and hand_name=="Right"):
                continue
            if hand_name == "Left":
                hand_detection_counts["Left"] += 2
                if hand_detection_counts["Left"] > g.config["Tracking"]["Hand"]["hand_detection_upper_threshold"]:
                    hand_detection_counts["Left"] = g.config["Tracking"]["Hand"]["hand_detection_upper_threshold"]
                if hand_detection_counts["Left"] <= g.config["Tracking"]["Hand"]["hand_detection_lower_threshold"]:
                    continue
            else:
                hand_detection_counts["Right"] += 2
                if hand_detection_counts["Right"] > g.config["Tracking"]["Hand"]["hand_detection_upper_threshold"]:
                    hand_detection_counts["Right"] = g.config["Tracking"]["Hand"]["hand_detection_upper_threshold"]
                if hand_detection_counts["Right"] <= g.config["Tracking"]["Hand"]["hand_detection_lower_threshold"]:
                    continue

            world_landmarks = hand_world_landmarks.landmark
            hand_pose = get_hand_pose(world_landmarks)
            image_landmarks = hand_landmarks.landmark
            image_hand_pose = get_hand_pose(image_landmarks, False)
            # hand_position = [g.data["HeadImagePosition"][0]["v"], g.data["HeadImagePosition"][1]["v"],
            #                  g.data["HeadImagePosition"][2]["v"]] - image_hand_pose[2]
            hand_position = [g.data["HeadImagePosition"][0]["v"], g.data["HeadImagePosition"][1]["v"],
                             g.data["HeadImagePosition"][2]["v"]] - image_hand_pose[9]
            hand_position[:2] *= [g.config["Tracking"]["Hand"]["x_scalar"], g.config["Tracking"]["Hand"]["y_scalar"]]
            # hand_distance_temp=np.linalg.norm(np.array(image_hand_pose[1][:2]) - np.array(image_hand_pose[2][:2]))
            # import keyboard
            # import pickle
            # if keyboard.is_pressed('a'):
            #     filename = f'./depth_dataset/image_hand_pose_{hand_name}_{index}.pkl'
            #     with open(filename, 'wb') as f:
            #         pickle.dump(image_hand_pose, f)
            #     print(f"image_hand_pose {filename}")
            #     index += 1

            keypoints = [5, 9, 13]
            data = image_hand_pose - image_hand_pose[0]
            data = data[keypoints].flatten()
            data = np.array(data)
            data = data.reshape(1, -1)  # Reshape to 2D array
            data_transformed = g.hand_feature_model.transform(data)
            pred_distance = g.hand_regression_model.predict(data_transformed)
            hand_distance_temp=pred_distance[0]

            rounded_value = np.round(g.data["HeadImagePosition"][2]["v"], 2)
            clipped_value = np.clip(rounded_value, None, -1e-8)
            distance_scalar = clipped_value

            hand_distance = hand_distance_temp/distance_scalar

            hand_distance += g.config["Tracking"]["Hand"]["z_shifting"]
            hand_distance *= g.config["Tracking"]["Hand"]["z_scalar"]
            hand_distance = np.interp(hand_distance, [-2, 2], [-1.2, 1])
            # print(hand_distance)
            if g.config["Tracking"]["Hand"]["only_front"]:
                hand_distance = np.clip(hand_distance, -0.8, 0.0)

            hand_position[2] = hand_distance

            position_change_flag,_,swap_flag = hand_is_changed("position",hand_name,hand_landmarks,g.config["Tracking"]["Hand"]["position_change_points"],g.config["Tracking"]["Hand"]["position_change_threshold"])
            if hand_name=="Left":
                g.controller.left_hand.change_flag=position_change_flag
            elif hand_name=="Right":
                g.controller.right_hand.change_flag=position_change_flag

            rotation_change_flag,_,_ = hand_is_changed("rotation",hand_name,hand_landmarks,g.config["Tracking"]["Hand"]["rotation_change_points"],g.config["Tracking"]["Hand"]["rotation_change_threshold"])
            if not g.config["Tracking"]["Pose"]["enable"]:
                if swap_flag and g.config["Tracking"]["Hand"]["enable_swap_strategy"]:
                    continue
            z = hand_pose[0] - hand_pose[17]
            x = np.cross(hand_pose[1] - hand_pose[0], z)
            y = np.cross(z, x)
            x = x / np.linalg.norm(x)
            y = y / np.linalg.norm(y)
            z = z / np.linalg.norm(z)

            wrist_matrix = np.vstack((x, y, z)).T
            wrist_rot = R.from_matrix(wrist_matrix).as_euler("xyz", degrees=True)

            # exit()
            if g.config["Tracking"]["Finger"]["enable"]:
                finger_curl=finger_handling(hand_pose)
                finger_0, finger_1, finger_2, finger_3, finger_4 = finger_curl["thumb"],finger_curl["index"],finger_curl["middle"],finger_curl["ring"],finger_curl["pinky"]
                if g.config["Tracking"]["Hand"]["enable_finger_action"]:
                    if finger_1 < g.config["Tracking"]["Hand"]["trigger_threshold"]:
                        g.controller.send_trigger(True if hand_name=="Left" else False, 0, 1)
                    else:
                        g.controller.send_trigger(True if hand_name=="Left" else False, 0, 0)
                    if finger_1>0.3 and finger_3>0.3 and finger_4 >0.5 and finger_0<0.7 and finger_2<0.4:
                        finger_action_threshold[hand_name] = g.config["Tracking"]["Hand"]["finger_action_threshold"]
                    else:
                        finger_action_threshold[hand_name] = max(0,finger_action_threshold[hand_name]-1)
                    if finger_action_threshold[hand_name] != 0:
                        finger_1 = 1.0
                        finger_3 = 1.0
                        finger_4 = 1.0
                        finger_0 = 0.0
                        finger_2 = 0.25

            else:
                finger_0, finger_1, finger_2, finger_3, finger_4 = 1.0, 1.0, 1.0, 1.0, 1.0

            if hand_name == "Left":
                if g.config["Smoothing"]["enable"]:
                    if rotation_change_flag:
                        g.latest_data[73] = wrist_rot[0]
                        g.latest_data[74] = wrist_rot[1]
                        g.latest_data[75] = wrist_rot[2]
                    if position_change_flag:
                        if not g.config["Tracking"]["Pose"]["enable"]:
                            g.latest_data[70] = hand_position[0]
                            g.latest_data[71] = hand_position[1]+g.config["Tracking"]["Hand"]["shift_l_y"]
                        g.latest_data[72] = hand_position[2]

                    g.latest_data[82] = finger_0
                    g.latest_data[83] = finger_1
                    g.latest_data[84] = finger_2
                    g.latest_data[85] = finger_3
                    g.latest_data[86] = finger_4
                else:
                    if rotation_change_flag:
                        g.data["LeftHandRotation"][0]["v"] = wrist_rot[0]
                        g.data["LeftHandRotation"][1]["v"] = wrist_rot[1]
                        g.data["LeftHandRotation"][2]["v"] = wrist_rot[2]
                    if position_change_flag:
                        if not g.config["Tracking"]["Pose"]["enable"]:
                            g.data["LeftHandPosition"][0]["v"] = hand_position[0]
                            g.data["LeftHandPosition"][1]["v"] = hand_position[1]+g.config["Tracking"]["Hand"]["shift_l_y"]
                        g.data["LeftHandPosition"][2]["v"] = hand_position[2]

                    g.data["LeftHandFinger"][0]["v"] = finger_0
                    g.data["LeftHandFinger"][1]["v"] = finger_1
                    g.data["LeftHandFinger"][2]["v"] = finger_2
                    g.data["LeftHandFinger"][3]["v"] = finger_3
                    g.data["LeftHandFinger"][4]["v"] = finger_4
                g.controller.left_hand.enable = True
            else:
                if g.config["Smoothing"]["enable"]:
                    if rotation_change_flag:
                        g.latest_data[79] = wrist_rot[0]
                        g.latest_data[80] = wrist_rot[1]
                        g.latest_data[81] = wrist_rot[2]
                    if position_change_flag:
                        if not g.config["Tracking"]["Pose"]["enable"]:
                            g.latest_data[76] = hand_position[0]
                            g.latest_data[77] = hand_position[1]+g.config["Tracking"]["Hand"]["shift_r_y"]
                        g.latest_data[78] = hand_position[2]

                    g.latest_data[87] = finger_0
                    g.latest_data[88] = finger_1
                    g.latest_data[89] = finger_2
                    g.latest_data[90] = finger_3
                    g.latest_data[91] = finger_4
                else:
                    if rotation_change_flag:
                        g.data["RightHandRotation"][0]["v"] = wrist_rot[0]
                        g.data["RightHandRotation"][1]["v"] = wrist_rot[1]
                        g.data["RightHandRotation"][2]["v"] = wrist_rot[2]
                    if position_change_flag:
                        if not g.config["Tracking"]["Pose"]["enable"]:
                            g.data["RightHandPosition"][0]["v"] = hand_position[0]
                            g.data["RightHandPosition"][1]["v"] = hand_position[1]+g.config["Tracking"]["Hand"]["shift_r_y"]
                        g.data["RightHandPosition"][2]["v"] = hand_position[2]

                    g.data["RightHandFinger"][0]["v"] = finger_0
                    g.data["RightHandFinger"][1]["v"] = finger_1
                    g.data["RightHandFinger"][2]["v"] = finger_2
                    g.data["RightHandFinger"][3]["v"] = finger_3
                    g.data["RightHandFinger"][4]["v"] = finger_4
                g.controller.right_hand.enable = True

    if hand_detection_counts["Left"] <= g.config["Tracking"]["Hand"]["hand_detection_lower_threshold"] and \
            g.config["Tracking"]["Hand"]["enable_hand_auto_reset"] and not g.config["Tracking"]["LeftController"][
        "enable"]:
        if g.config["Smoothing"]["enable"]:
            g.latest_data[73] = g.default_data["LeftHandRotation"][0]["v"]
            g.latest_data[74] = g.default_data["LeftHandRotation"][1]["v"]
            g.latest_data[75] = g.default_data["LeftHandRotation"][2]["v"]

            g.latest_data[70] = g.default_data["LeftHandPosition"][0]["v"]
            g.latest_data[71] = g.default_data["LeftHandPosition"][1]["v"]
            g.latest_data[72] = g.default_data["LeftHandPosition"][2]["v"]

            g.latest_data[82] = g.default_data["LeftHandFinger"][0]["v"]
            g.latest_data[83] = g.default_data["LeftHandFinger"][1]["v"]
            g.latest_data[84] = g.default_data["LeftHandFinger"][2]["v"]
            g.latest_data[85] = g.default_data["LeftHandFinger"][3]["v"]
            g.latest_data[86] = g.default_data["LeftHandFinger"][4]["v"]
        else:
            g.data["LeftHandPosition"] = deepcopy(g.default_data["LeftHandPosition"])
            g.data["LeftHandRotation"] = deepcopy(g.default_data["LeftHandRotation"])
            g.data["LeftHandFinger"] = deepcopy(g.default_data["LeftHandFinger"])
        if g.config["Tracking"]["Hand"]["follow"]:
            g.controller.left_hand.follow = False
        else:
            # g.controller.left_hand.follow = True
            g.controller.left_hand.follow = False
        g.controller.left_hand.enable = False
    else:
        # g.controller.left_hand.follow = True
        g.controller.left_hand.follow = False


    if hand_detection_counts["Right"] <= g.config["Tracking"]["Hand"]["hand_detection_lower_threshold"] and \
            g.config["Tracking"]["Hand"]["enable_hand_auto_reset"] and not g.config["Tracking"]["RightController"]["enable"]:
        if g.config["Smoothing"]["enable"]:
            g.latest_data[79] = g.default_data["RightHandRotation"][0]["v"]
            g.latest_data[80] = g.default_data["RightHandRotation"][1]["v"]
            g.latest_data[81] = g.default_data["RightHandRotation"][2]["v"]

            g.latest_data[76] = g.default_data["RightHandPosition"][0]["v"]
            g.latest_data[77] = g.default_data["RightHandPosition"][1]["v"]
            g.latest_data[78] = g.default_data["RightHandPosition"][2]["v"]

            g.latest_data[87] = g.default_data["RightHandFinger"][0]["v"]
            g.latest_data[88] = g.default_data["RightHandFinger"][1]["v"]
            g.latest_data[89] = g.default_data["RightHandFinger"][2]["v"]
            g.latest_data[90] = g.default_data["RightHandFinger"][3]["v"]
            g.latest_data[91] = g.default_data["RightHandFinger"][4]["v"]
        else:
            g.data["RightHandPosition"] = deepcopy(g.default_data["RightHandPosition"])
            g.data["RightHandRotation"] = deepcopy(g.default_data["RightHandRotation"])
            g.data["RightHandFinger"] = deepcopy(g.default_data["RightHandFinger"])
        if g.config["Tracking"]["Hand"]["follow"]:
            g.controller.right_hand.follow = False
        else:
            # g.controller.right_hand.follow = True
            g.controller.right_hand.follow = False
        g.controller.right_hand.enable = False
    else:
        # g.controller.right_hand.follow = True
        g.controller.right_hand.follow = False

# class HandDetector:

#     def __init__(self):
#         mp_hands = mp.solutions.hands
#         self.hands = mp_hands.Hands(
#             model_complexity         = g.config["Model"]["Hand"]["model_complexity"],
#             max_num_hands            = 2,
#             min_detection_confidence = g.config["Model"]["Hand"]["min_hand_detection_confidence"],
#             min_tracking_confidence  = g.config["Model"]["Hand"]["min_tracking_confidence"]
#         )
#
#         self._frame_queue = queue.Queue(maxsize=1)
#         self._stop_event  = threading.Event()
#         self._worker      = threading.Thread(
#             target=self._worker_loop,
#             daemon=True,
#             name="HandWorker"
#         )
#         self._worker.start()
#
#     def detect_async(self, image_bgr: np.ndarray) -> None:
#         if self._frame_queue.full():
#             try:
#                 _ = self._frame_queue.get_nowait()   # 丢弃旧帧
#             except queue.Empty:
#                 pass
#         self._frame_queue.put_nowait(image_bgr)
#
#     def close(self):
#         if not self._stop_event.is_set():
#             self._stop_event.set()
#             if self._worker.is_alive():
#                 self._worker.join(timeout=1.0)
#         if hasattr(self, "hands"):
#             self.hands.close()
#
#     def __del__(self):
#         self.close()
#
#     def _worker_loop(self):
#         while not self._stop_event.is_set():
#             try:
#                 frame_bgr = self._frame_queue.get(timeout=0.02)
#             except queue.Empty:
#                 continue
#
#             frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
#             result    = self.hands.process(frame_rgb)
#
#             hand_pred_handling(result)

def initialize_hand():
    mp_hands = mp.solutions.hands
    return mp_hands.Hands(model_complexity=g.config["Model"]["Hand"]["model_complexity"], max_num_hands=2,
                          min_detection_confidence=g.config["Model"]["Hand"]["min_hand_detection_confidence"],
                          min_tracking_confidence=g.config["Model"]["Hand"]["min_tracking_confidence"])
# def initialize_hand():
#     return HandDetector()

def initialize_hand_depth():
    feature_model = joblib.load('./models/hand_feature_model.pkl')
    hand_regression_model = joblib.load('./models/hand_regression_model.pkl')
    return feature_model, hand_regression_model
