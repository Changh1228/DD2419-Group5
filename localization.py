#!/usr/bin/env python

import math
import sys
import json
import numpy as np
import rospy
import tf2_ros
import tf2_geometry_msgs
from tf.transformations import euler_from_quaternion, quaternion_from_euler, quaternion_matrix, euler_from_matrix
from geometry_msgs.msg import PoseStamped, TransformStamped, Vector3
from aruco_msgs.msg import MarkerArray
from crazyflie_driver.msg import Position

import copy
from collections import deque

aruco_queue = deque(maxlen = 6)

'''
dangerous when yaw of drift close to 180 degree
no uncertainty now
'''

def transform_callback(data):
    # update pose of aruco (camera_link)
    global aruco_queue
    aruco_queue.append(data)


def read_from_json(m):
    t = PoseStamped()
    t.header.stamp = rospy.Time.now()
    t.header.frame_id = 'map'
    t.pose.position = Vector3(*m['pose']['position'])
    roll, pitch, yaw = m['pose']['orientation']
    (t.pose.orientation.x,
     t.pose.orientation.y,
     t.pose.orientation.z,
     t.pose.orientation.w) = quaternion_from_euler(math.radians(roll),
                                                   math.radians(pitch),
                                                   math.radians(yaw))
    return t


def pre_aruco(aruco_queue):
    '''preprocess the data form camera: delete the outlier and choose nearest markers'''
    k = len(aruco_queue)
    if k < 6:  # if no enough data in queue, quit
        return None, None
    # choose nearest marker
    record = np.zeros(16)
    for data in aruco_queue: # data in same timestamp
        d = 10000
        id = None
        for aruco in data.markers:  # for all markers
            if aruco.id > 15:
                continue
            d_now = pow(aruco.pose.pose.position.x, 2) + pow(aruco.pose.pose.position.y, 2) + pow(aruco.pose.pose.position.z, 2)
            if d_now < d:
                d = d_now  # max distance and its ID
                id = aruco.id
        record[id] += 1
    nearest_ID = np.where(record==np.max(record))  # nearest Aruco ID
    nearest_ID = nearest_ID[0][0]

    # delete the outlier (largest and smallest distance)
    # and average nearest_ID
    distance = np.zeros(k)
    t = 0
    for data in aruco_queue: # data in same timestamp
        for aruco in data.markers:  # for all markers
            if aruco.id == nearest_ID:  # only care about nearest marker
                distance[t] = pow(aruco.pose.pose.position.x, 2) + pow(aruco.pose.pose.position.y, 2)
        t += 1
    largest_ID = np.where(distance==np.max(distance))  # largest timestamp
    largest_ID = largest_ID[0][0]
    smallest_ID = np.where(distance==np.min(distance))  # smallest timestamp
    smallest_ID = smallest_ID[0][0]

    # average
    t = 0
    p_x = 0
    p_y = 0
    p_z = 0
    o_r = 0
    o_p = 0
    o_y = 0

    for data in aruco_queue: # data in same timestamp
        if t!=largest_ID and t!=smallest_ID:
            for aruco in data.markers:  # for all markers
                if aruco.id == nearest_ID:  # only care about nearest marker
                    p_x += aruco.pose.pose.position.x
                    p_y += aruco.pose.pose.position.y
                    p_z += aruco.pose.pose.position.z
                    roll, pitch, yaw = euler_from_quaternion((aruco.pose.pose.orientation.x,
                                                              aruco.pose.pose.orientation.y,
                                                              aruco.pose.pose.orientation.z,
                                                              aruco.pose.pose.orientation.w))
                    o_r += roll
                    o_p += pitch
                    o_y += yaw

        t += 1

    p_x /= k-2
    p_y /= k-2
    p_z /= k-2
    o_r /= k-2
    o_p /= k-2
    o_y /= k-2
    (o_x, o_y, o_z, o_w) = quaternion_from_euler(roll, pitch, yaw)

    result = PoseStamped()
    result.header.stamp = aruco_queue[int(k/2)].header.stamp  # use time at middle stamp
    result.header.frame_id = "cf1/camera_link"
    result.pose.position.x = p_x
    result.pose.position.y = p_y
    result.pose.position.z = p_z
    result.pose.orientation.x = o_x
    result.pose.orientation.y = o_y
    result.pose.orientation.z = o_z
    result.pose.orientation.w = o_w

    if p_x == 0 and p_y == 0:
        print("error")
        return None, None
    return result, nearest_ID


def localization(argv):

    tf_buf   = tf2_ros.Buffer()
    tf_lstn  = tf2_ros.TransformListener(tf_buf)

    global aruco_queue
    d_buf = None
    yaw_buf = None

    queue_len = 6
    queue = deque(maxlen = queue_len) # make a queue length of 10 for time average

    # Load world JSON
    args = rospy.myargv(argv=argv)  # Let ROS filter through the arguments
    with open(args[1], 'rb') as f:
        world = json.load(f)

    # Get pose of aruco in map frame (accurate)
	aruco_map_pose = {m["id"]: read_from_json(m) for m in world['markers']}

    rate = rospy.Rate(10)  # Hz
    print('localization running')
    t = 0 # flag of average
    while not rospy.is_shutdown():
        # deepcopy to prevent value change when preprocess
        aruco_marker, aruco_ID = pre_aruco(copy.deepcopy(aruco_queue))

        if aruco_marker == None:  # no enough data form queue
            continue
        if (aruco_marker.header.stamp.secs - rospy.Time.now().secs) > 2:  # data form 2s before, reject
            continue

        # ---------------do localization ----------------
        # transform pose in camera link to odm link
        if not tf_buf.can_transform(aruco_marker.header.frame_id, 'cf1/odom', aruco_marker.header.stamp):
            rospy.logwarn_throttle(5.0, 'No transform from %s to cf1/odom' % aruco_marker.header.frame_id)
            aruco_odm_pose = tf_buf.transform(aruco_marker, 'cf1/odom', rospy.Duration(1.0))
        #else:
        aruco_odm_pose = tf_buf.transform(aruco_marker, 'cf1/odom')

        # calculate the difference (true_pose detect_pose)
        quat = [aruco_map_pose[aruco_ID].pose.orientation.x,
                aruco_map_pose[aruco_ID].pose.orientation.y,
                aruco_map_pose[aruco_ID].pose.orientation.z,
                aruco_map_pose[aruco_ID].pose.orientation.w]
        T_m2a = quaternion_matrix(quat)
        T_m2a[0][3] = aruco_map_pose[aruco_ID].pose.position.x
        T_m2a[1][3] = aruco_map_pose[aruco_ID].pose.position.y
        T_m2a[2][3] = aruco_map_pose[aruco_ID].pose.position.z

        quat2 = [aruco_odm_pose.pose.orientation.x,
                 aruco_odm_pose.pose.orientation.y,
                 aruco_odm_pose.pose.orientation.z,
                 aruco_odm_pose.pose.orientation.w]
        T_o2a = quaternion_matrix(quat2)
        T_o2a[0][3] = aruco_odm_pose.pose.position.x
        T_o2a[1][3] = aruco_odm_pose.pose.position.y
        T_o2a[2][3] = aruco_odm_pose.pose.position.z

        T_inv = np.linalg.inv(T_o2a)
        T_m2o = np.matmul(T_m2a, T_inv)
        position_x = T_m2o[0][3]
        position_y = T_m2o[1][3]
        position_z = T_m2o[2][3]

        T_m2o[0][3] = 0
        T_m2o[1][3] = 0
        T_m2o[2][3] = 0

        (roll, pitch, yaw) = euler_from_matrix(T_m2o, 'rxyz')

        # cal time average
        queue.append([position_x, position_y, yaw])
        k = len(queue)
        timeAve_x = 0
        timeAve_y = 0
        timeAve_yaw = 0

        for i in queue:
            timeAve_x +=  i[0]
            timeAve_y +=  i[1]
            timeAve_yaw +=  i[2]

        timeAve_x /= k
        timeAve_y /= k
        timeAve_yaw /= k

        if not d_buf:
            d_buf = [0, 0, 0]  # x, y, yaw of last tf

        step_d = math.sqrt(pow(timeAve_x - d_buf[0], 2) + pow(timeAve_y - d_buf[1], 2))
        step_yaw = math.degrees(abs(timeAve_yaw - d_buf[2]))

        if (step_d > 0.1 or step_yaw > 5) and t >= queue_len:
            (orientation_x, orientation_y, orientation_z, orientation_w) = quaternion_from_euler(0.000001,
                                                                                                 0.000001,
                                                                                                 timeAve_yaw)
            # TF form odm to map
            br = tf2_ros.TransformBroadcaster()
            tfO2M = TransformStamped()
            tfO2M.header.stamp = rospy.Time.now()
            tfO2M.header.frame_id = "map"
            tfO2M.child_frame_id = "cf1/odom"
            tfO2M.transform.translation.x = timeAve_x
            tfO2M.transform.translation.y = timeAve_y
            tfO2M.transform.translation.z = 0.0
            tfO2M.transform.rotation.x = orientation_x
            tfO2M.transform.rotation.y = orientation_y
            tfO2M.transform.rotation.z = orientation_z
            tfO2M.transform.rotation.w = orientation_w
            br.sendTransform(tfO2M)
            '''print("publish tf")
            print("timeAve_yaw = %f" % yaw)
            print("timeAve_x = %f" % position_x)
            print("timeAve_y = %f" % position_y)'''
            d_buf = [position_x, position_y, yaw]
            aruco_queue.clear()
            t = 0
        t += 1
        rate.sleep()



def main(argv=sys.argv):

    rospy.init_node('localization', anonymous=True)
    rospy.Subscriber('/aruco/markers', MarkerArray, transform_callback)

    localization(argv)


if __name__ == '__main__':
    main()