#!/usr/bin/env python
from copy import deepcopy

import rospy

from std_msgs.msg import Header, String
from sensor_msgs.msg import LaserScan
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, PoseArray, Pose, Point, Quaternion
from nav_msgs.srv import GetMap

import tf
from tf import TransformListener
from tf import TransformBroadcaster
from tf.transformations import euler_from_quaternion, rotation_matrix, quaternion_from_matrix
from random import gauss

import math
import time

import numpy as np
from scipy.stats import norm
from numpy.random import random_sample
from sklearn.neighbors import NearestNeighbors
import matplotlib.pyplot as plt


class TransformHelpers:
    """ Some convenience functions for translating between various
        representions of a robot pose.
        TODO: nothing... you should not have to modify these """

    @staticmethod
    def convert_translation_rotation_to_pose(translation, rotation):
        """ Convert from representation of a pose as translation and rotation
        (Quaternion) tuples to a geometry_msgs/Pose message """
        return Pose(position=Point(x=translation[0], y=translation[1],
                                   z=translation[2]),
                    orientation=Quaternion(x=rotation[0], y=rotation[1],
                                           z=rotation[2], w=rotation[3]))

    @staticmethod
    def convert_pose_inverse_transform(pose):
        """ Helper method to invert a transform (this is built into the tf
            C++ classes, but ommitted from Python) """
        translation = np.zeros((4, 1))
        translation[0] = -pose.position.x
        translation[1] = -pose.position.y
        translation[2] = -pose.position.z
        translation[3] = 1.0

        rotation = (pose.orientation.x, pose.orientation.y,
                    pose.orientation.z, pose.orientation.w)
        euler_angle = euler_from_quaternion(rotation)
        rotation = np.transpose(rotation_matrix(euler_angle[2], [0, 0, 1]))
        transformed_translation = rotation.dot(translation)

        translation = (transformed_translation[0], transformed_translation[1],
                       transformed_translation[2])
        rotation = quaternion_from_matrix(rotation)
        return (translation, rotation)

    @staticmethod
    def convert_pose_to_xy_and_theta(pose):
        """ Convert pose (geometry_msgs.Pose) to a (x,y,yaw) tuple """
        orientation_tuple = (pose.orientation.x, pose.orientation.y,
                             pose.orientation.z, pose.orientation.w)
        angles = euler_from_quaternion(orientation_tuple)
        return (pose.position.x, pose.position.y, angles[2])


class Particle:
    """ Represents a hypothesis (particle) of the robot's pose consisting
        of x,y and theta (yaw)
        Attributes:
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of the hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle
                weights are normalized
    """

    def __init__(self, x=0.0, y=0.0, theta=0.0, w=1.0):
        """ Construct a new Particle
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of the hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle
                weights are normalized """
        self.x = x
        self.y = y
        self.theta = theta
        self.w = w

    def as_pose(self):
        """ A helper function to convert a particle to a geometry_msgs/Pose
        message """
        orientation_tuple = tf.transformations.quaternion_from_euler(0, 0, self.theta)
        return Pose(position=Point(x=self.x, y=self.y, z=0),
                    orientation=Quaternion(x=orientation_tuple[0], y=orientation_tuple[1], z=orientation_tuple[2],
                                           w=orientation_tuple[3]))

    # TODO: define additional helper functions if needed


""" Difficulty Level 2 """


class OccupancyField:
    """ Stores an occupancy field for an input map.  An occupancy field returns the distance to the closest
        obstacle for any coordinate in the map
        Attributes:
            map: the map to localize against (nav_msgs/OccupancyGrid)
            closest_occupied: the distance for each entry in the OccupancyGrid to the closest obstacle
    """

    def __init__(self, map):
        self.map = map  # save this for later
        # build up a numpy array of the coordinates of each grid cell in the map
        cell_coordinates = np.zeros((self.map.info.width * self.map.info.height, 2))

        # while we're at it let's count the number of occupied cells
        total_occupied = 0
        curr = 0
        for i in range(self.map.info.width):
            for j in range(self.map.info.height):
                # occupancy grids are stored in row major order, if you go through this right, you might be able to use curr
                ind = i + j * self.map.info.width
                if self.map.data[ind] > 0:
                    total_occupied += 1
                cell_coordinates[curr, 0] = float(i)
                cell_coordinates[curr, 1] = float(j)
                curr += 1

        # build up a numpy array of the coordinates of each occupied grid cell in the map
        occupied_cell_coordinates = np.zeros((total_occupied, 2))
        curr = 0
        for i in range(self.map.info.width):
            for j in range(self.map.info.height):
                # occupancy grids are stored in row major order, if you go through this right, you might be able to use curr
                ind = i + j * self.map.info.width
                if self.map.data[ind] > 0:
                    occupied_cell_coordinates[curr, 0] = float(i)
                    occupied_cell_coordinates[curr, 1] = float(j)
                    curr += 1
        # self.occupied_cell_coordinates = occupied_cell_coordinates

        # use super fast scikit learn nearest neighbor algorithm
        neighbors = NearestNeighbors(n_neighbors=1, algorithm="ball_tree").fit(occupied_cell_coordinates)
        distances, indices = neighbors.kneighbors(cell_coordinates)

        self.closest_occupied = {}
        curr = 0
        for i in range(self.map.info.width):
            for j in range(self.map.info.height):
                ind = i + j * self.map.info.width
                self.closest_occupied[ind] = distances[curr][0] * self.map.info.resolution
                curr += 1

    def get_closest_obstacle_distance(self, x, y):
        """ Compute the closest obstacle to the specified (x,y) coordinate in the map.  If the (x,y) coordinate
            is out of the map boundaries, nan will be returned. """
        x_coord = int((x - self.map.info.origin.position.x) / self.map.info.resolution)
        y_coord = int((y - self.map.info.origin.position.y) / self.map.info.resolution)

        # check if we are in bounds
        if x_coord > self.map.info.width or x_coord < 0:
            return float('nan')
        if y_coord > self.map.info.height or y_coord < 0:
            return float('nan')

        ind = x_coord + y_coord * self.map.info.width
        if ind >= self.map.info.width * self.map.info.height or ind < 0:
            return float('nan')
        return self.closest_occupied[ind]


class ParticleFilter:
    """ The class that represents a Particle Filter ROS Node
        Attributes list:
            initialized: a Boolean flag to communicate to other class methods that initializaiton is complete
            base_frame: the name of the robot base coordinate frame (should be "base_link" for most robots)
            map_frame: the name of the map coordinate frame (should be "map" in most cases)
            odom_frame: the name of the odometry coordinate frame (should be "odom" in most cases)
            scan_topic: the name of the scan topic to listen to (should be "scan" in most cases)
            n_particles: the number of particles in the filter
            d_thresh: the amount of linear movement before triggering a filter update
            a_thresh: the amount of angular movement before triggering a filter update
            laser_max_distance: the maximum distance to an obstacle we should use in a likelihood calculation
            pose_listener: a subscriber that listens for new approximate pose estimates (i.e. generated through the rviz GUI)
            particle_pub: a publisher for the particle cloud
            laser_subscriber: listens for new scan data on topic self.scan_topic
            tf_listener: listener for coordinate transforms
            tf_broadcaster: broadcaster for coordinate transforms
            particle_cloud: a list of particles representing a probability distribution over robot poses
            current_odom_xy_theta: the pose of the robot in the odometry frame when the last filter update was performed.
                                   The pose is expressed as a list [x,y,theta] (where theta is the yaw)
            map: the map we will be localizing ourselves in.  The map should be of type nav_msgs/OccupancyGrid
            robot_pose: estimated position of the robot of type geometry_msgs/Pose
    """

    # some constants! :) -emily and franz
    TAU = math.pi * 2.0
    # to be used in update_particles_with_odom
    RADIAL_SIGMA = .03 # meters
    ORIENTATION_SIGMA = 0.03 * TAU

    def __init__(self):
        self.initialized = False  # make sure we don't perform updates before everything is setup
        rospy.init_node('pf')  # tell roscore that we are creating a new node named "pf"

        self.base_frame = "base_link"  # the frame of the robot base
        self.map_frame = "map"  # the name of the map coordinate frame
        self.odom_frame = "odom"  # the name of the odometry coordinate frame
        self.scan_topic = "scan"  # the topic where we will get laser scans from

        self.n_particles = 30  # the number of particles to use

        self.d_thresh = 0.04  # the amount of linear movement before performing an update
        self.a_thresh = 0.04 * ParticleFilter.TAU  # the amount of angular movement before performing an update

        self.laser_max_distance = 2.0  # maximum penalty to assess in the likelihood field model

        # TODO: define additional constants if needed

        # Setup pubs and subs

        # pose_listener responds to selection of a new approximate robot location (for instance using rviz)
        self.pose_listener = rospy.Subscriber("initialpose", PoseWithCovarianceStamped, self.update_initial_pose)
        # publish the current particle cloud.  This enables viewing particles in rviz.
        self.rawcloud_pub = rospy.Publisher("rawcloud", PoseArray, queue_size=1)
        self.odomcloud_pub = rospy.Publisher("odomcloud", PoseArray, queue_size=1)
        self.lasercloud_pub = rospy.Publisher("lasercloud", PoseArray, queue_size=1)
        self.resamplecloud_pub = rospy.Publisher("resamplecloud", PoseArray, queue_size=1)
        self.finalcloud_pub = rospy.Publisher("finalcloud", PoseArray, queue_size=1)

        # laser_subscriber listens for data from the lidar
        self.laser_subscriber = rospy.Subscriber(self.scan_topic, LaserScan, self.scan_received)

        # enable listening for and broadcasting coordinate transforms
        self.tf_listener = TransformListener()
        self.tf_broadcaster = TransformBroadcaster()

        self.particle_cloud = []

        self.current_odom_xy_theta = []

        # request the map from the map server, the map should be of type nav_msgs/OccupancyGrid
        get_static_map = rospy.ServiceProxy('static_map', GetMap)
        self.occupancy_field = OccupancyField(get_static_map().map)
        self.robot_pose = Pose()
        self.initialized = True

    def update_robot_pose(self):
        """ Update the estimate of the robot's pose given the updated particles.
            There are two logical methods for this:
                (1): compute the mean pose (level 2)
                (2): compute the most likely pose (i.e. the mode of the distribution) (level 1)
        """

        # first make sure that the particle weights are normalized
        self.normalize_particles()

        # compute mean pose by calculating the weighted average of each position and angle
        mean_x = 0
        mean_y = 0
        mean_theta = 0
        for particle in self.particle_cloud:
            mean_x += particle.w * particle.x
            mean_y += particle.w * particle.y
            mean_theta += particle.w * particle.theta
        mean_particle = Particle(mean_x, mean_y, mean_theta)
        self.robot_pose = mean_particle.as_pose()

    def update_particles_with_odom(self, msg):
        """ Implement a simple version of this (Level 1) or a more complex one (Level 2) """
        new_odom_xy_theta = TransformHelpers.convert_pose_to_xy_and_theta(self.odom_pose.pose)

        # compute the change in x,y,theta since our last update
        if self.current_odom_xy_theta:
            old_odom_xy_theta = self.current_odom_xy_theta
            delta = (
                new_odom_xy_theta[0] - self.current_odom_xy_theta[0], new_odom_xy_theta[1] - self.current_odom_xy_theta[1],
                new_odom_xy_theta[2] - self.current_odom_xy_theta[2])
            self.current_odom_xy_theta = new_odom_xy_theta
        else:
            self.current_odom_xy_theta = new_odom_xy_theta
            return

        r1 = math.atan2(delta[1], delta[0]) - old_odom_xy_theta[2]
        delta_distance = np.linalg.norm([delta[0], delta[1]])
        r2 = delta[2] - r1

        for particle in self.particle_cloud:
            # randomly pick the deltas for radial distance, mean angle, and orientation angle
            delta_random_radius = np.random.normal(0, ParticleFilter.RADIAL_SIGMA)
            delta_random_mean_angle = random_sample() * ParticleFilter.TAU / 2.0
            delta_random_orient_angle = np.random.normal(0, ParticleFilter.ORIENTATION_SIGMA)

            # calculate the deltas
            delta_random_x = delta_random_radius * math.cos(delta_random_mean_angle)
            delta_random_y = delta_random_radius * math.sin(delta_random_mean_angle)

            # update the mean (add deltas)
            particle.theta += r1
            particle.x += math.cos(particle.theta) * delta_distance + delta_random_x
            particle.y += math.sin(particle.theta) * delta_distance + delta_random_y
            particle.theta += r2 + delta_random_orient_angle

        # For added difficulty: Implement sample_motion_odometry (Prob Rob p 136)

    def map_calc_range(self, x, y, theta):
        """ Difficulty Level 3: implement a ray tracing likelihood model... Let me know if you are interested """
        # TODO: nothing unless you want to try this alternate likelihood model
        pass

    def resample_particles(self):
        """ Resample the particles according to the new particle weights """
        # make sure the distribution is normalized
        self.normalize_particles()
        probabilities = [particle.w for particle in self.particle_cloud]

        new_particle_cloud = []
        for i in range(self.n_particles):
            random_particle = deepcopy(np.random.choice(self.particle_cloud, p=probabilities))
            new_particle_cloud.append(random_particle)

        self.particle_cloud = new_particle_cloud

    def update_particles_with_laser(self, msg):
        """ Updates the particle weights in response to the scan contained in the msg """
        valid_ranges = self.filter_laser(msg.ranges)
        keys = valid_ranges.keys()
        valid_len = len(keys)
        num_pt_check = 25

        # p1 * p2 ... p360
        # ( p1 + p2 ... p360 )/ 360
        # log(p1) + log(p2) ... log(p360)
        # p1^3 + p2^3 ... p360^3
        # Instead of norm(x = closest_occ) do 
        #   (1/2) norm(mean = 0, sigma = laser_variance, x = closest_occ)
        #   + (1/2) norm(mean = 0, sigma = laser_variance, x = range)

        for particle in self.particle_cloud:
            density_product = 1
            # density_sum = 0

            if valid_len >= num_pt_check:
                for i in range(num_pt_check):
                    index = keys[int(i * valid_len / num_pt_check)]
                    radius = valid_ranges[index]
                    angle = (index + .25 * ParticleFilter.TAU) % ParticleFilter.TAU
                    x = math.cos(angle + particle.theta) * radius + particle.x
                    y = math.sin(angle + particle.theta) * radius + particle.y
                    dist_to_nearest_neighbor = self.occupancy_field.get_closest_obstacle_distance(x, y)
                    # calculate probability of nearest neighbor's distance
                    # adding a constant to represent a constant chance
                    # that the measurement is invalid
                    probability_density = norm.pdf(loc = 0, scale = 0.05,
                                                   x = dist_to_nearest_neighbor) # + 0.1

                    # logpd = math.log( norm.pdf(loc=0, scale=.05, x=dist_to_nearest_neighbor) )

                    density_product *= 1 + probability_density #the 1+ is hacky
                    # density_sum += probability_density
                    # density_sum += logpd

                    # TODO: make the total_probability_density function more legit
                particle.w = density_product
                # particle.w = density_sum

    def visualize_p_weights(self):
        """ Produces a plot of particle weights vs. x position """
        # close any figures that are open
        plt.close('all')

        # initialize the things
        xpos = np.zeros(len(self.particle_cloud))
        weights = np.zeros(len(self.particle_cloud))
        x_i = 0
        weights_i = 0

        # grab the current values
        for p in self.particle_cloud:
            xpos[x_i] = p.x 
            weights[weights_i] = p.w

            x_i += 1
            weights_i += 1

        # plotting current xpos and weights
        fig = plt.figure()
        plt.xlabel('xpos')
        plt.ylabel('weights')
        plt.title('xpos vs weights')
        plt.plot(xpos, weights, 'ro')
        plt.show(block=False)

    @staticmethod
    def angle_normalize(z):
        """ convenience function to map an angle to the range [-pi,pi] """
        return math.atan2(math.sin(z), math.cos(z))

    @staticmethod
    def angle_diff(a, b):
        """ Calculates the difference between angle a and angle b (both should be in radians)
            the difference is always based on the closest rotation from angle a to angle b
            examples:
                angle_diff(.1,.2) -> -.1
                angle_diff(.1, 2*math.pi - .1) -> .2
                angle_diff(.1, .2+2*math.pi) -> -.1
        """
        a = ParticleFilter.angle_normalize(a)
        b = ParticleFilter.angle_normalize(b)
        d1 = a - b
        d2 = 2 * math.pi - math.fabs(d1)
        if d1 > 0:
            d2 *= -1.0
        if math.fabs(d1) < math.fabs(d2):
            return d1
        else:
            return d2

    @staticmethod
    def weighted_values(values, probabilities, size):
        """ Return a random sample of size elements form the set values with the specified probabilities
            values: the values to sample from (numpy.ndarray)
            probabilities: the probability of selecting each element in values (numpy.ndarray)
            size: the number of samples
        """
        bins = np.add.accumulate(probabilities)
        return values[np.digitize(random_sample(size), bins)]

    def update_initial_pose(self, msg):
        """ Callback function to handle re-initializing the particle filter based on a pose estimate.
            These pose estimates could be generated by another ROS Node or could come from the rviz GUI """
        xy_theta = TransformHelpers.convert_pose_to_xy_and_theta(msg.pose.pose)
        self.initialize_particle_cloud(xy_theta)
        self.fix_map_to_odom_transform(msg)

    def initialize_particle_cloud(self):
        """ Initialize the particle cloud.
            Arguments
            """
        rospy.loginfo("initialize particle cloud")
        self.particle_cloud = []
        map_info = self.occupancy_field.map.info
        for i in range(self.n_particles):
            x = random_sample() * map_info.width * map_info.resolution * 0.05
            if random_sample() > 0.5:
                x = -x
            # x = 0
            y = random_sample()* map_info.height * map_info.resolution * 0.1 
            if random_sample() > 0.5:
                y = -y
            # theta = random_sample() * math.pi*2
            theta = 0.34 * ParticleFilter.TAU
            # y = math.sin(theta - 0.25 * ParticleFilter.TAU) * x
            self.particle_cloud.append(Particle(x, y, theta))

        self.normalize_particles()
        self.update_robot_pose()

    def normalize_particles(self):
        """ Make sure the particle weights define a valid distribution (i.e.
            sum to 1.0) """
        sum = 0
        for particle in self.particle_cloud:
            sum += particle.w
        for particle in self.particle_cloud:
            particle.w /= sum

    def publish_particles(self, pub):
        particles_conv = []
        for p in self.particle_cloud:
            particles_conv.append(p.as_pose())
        # actually send the message so that we can view it in rviz
        pub.publish(
            PoseArray(header=Header(stamp=rospy.Time.now(), frame_id=self.map_frame), poses=particles_conv))

    def scan_received(self, msg):
        """ This is the default logic for what to do when processing scan data.  Feel free to modify this, however,
            I hope it will provide a good guide.  The input msg is an object of type sensor_msgs/LaserScan """

        rospy.logwarn("Got new data!")

        if not self.initialized:
            # wait for initialization to complete
            rospy.logwarn("ParticleFilter class isn't yet initialized")
            return

        if not (self.tf_listener.canTransform(self.base_frame, msg.header.frame_id, rospy.Time(0))):
            # need to know how to transform the laser to the base frame
            # this will be given by either Gazebo or neato_node
            rospy.logwarn("can't transform to laser scan")
            return

        if not (self.tf_listener.canTransform(self.base_frame, self.odom_frame, rospy.Time(0))):
            # need to know how to transform between base and odometric frames
            # this will eventually be published by either Gazebo or neato_node
            rospy.logwarn("can't transform to base frame")
            return

        # calculate pose of laser relative ot the robot base
        p = PoseStamped(header = Header(stamp = rospy.Time(0),
                                        frame_id = msg.header.frame_id))
        self.laser_pose = self.tf_listener.transformPose(self.base_frame, p)

        # find out where the robot thinks it is based on its odometry
        p = PoseStamped(header = Header(stamp = rospy.Time(0),
                                        frame_id = self.base_frame))
        self.odom_pose = self.tf_listener.transformPose(self.odom_frame, p)
        # store the the odometry pose in a more convenient format (x,y,theta)
        new_odom_xy_theta = TransformHelpers.convert_pose_to_xy_and_theta(self.odom_pose.pose)

        if not self.particle_cloud:
            # now that we have all of the necessary transforms we can update the particle cloud
            self.initialize_particle_cloud()
            # cache the last odometric pose so we can only update our particle filter if we move more than self.d_thresh or self.a_thresh
            self.current_odom_xy_theta = new_odom_xy_theta
            # update our map to odom transform now that the particles are initialized
            self.fix_map_to_odom_transform(msg)
        elif (math.fabs(new_odom_xy_theta[0] - self.current_odom_xy_theta[0]) > self.d_thresh or
                      math.fabs(new_odom_xy_theta[1] - self.current_odom_xy_theta[1]) > self.d_thresh or
                      math.fabs(new_odom_xy_theta[2] - self.current_odom_xy_theta[2]) > self.a_thresh):
            # we have moved far enough to do an update!
            self.publish_particles(self.rawcloud_pub)
            
            self.update_particles_with_odom(msg)  # update based on odometry
            self.publish_particles(self.odomcloud_pub)
            

            start_time = time.time()
            self.update_particles_with_laser(msg)  # update based on laser scan
            end_time = time.time()
            rospy.loginfo("Update with laser took %f seconds", end_time - start_time)


            self.publish_particles(self.lasercloud_pub)

            self.resample_particles()  # resample particles to focus on areas of high density
            self.update_robot_pose()  # update robot's pose
            self.fix_map_to_odom_transform(msg)  # update map to odom transform now that we have new particles
            # self.visualize_p_weights()

        # publish particles (so things like rviz can see them)
        self.publish_particles(self.finalcloud_pub)

    def fix_map_to_odom_transform(self, msg):
        """ Super tricky code to properly update map to odom transform... do not modify this... Difficulty level infinity. """
        (translation, rotation) = TransformHelpers.convert_pose_inverse_transform(self.robot_pose)
        p = PoseStamped(pose=TransformHelpers.convert_translation_rotation_to_pose(translation, rotation),
                        header=Header(stamp=rospy.Time(0), frame_id=self.base_frame))
        self.odom_to_map = self.tf_listener.transformPose(self.odom_frame, p)
        (self.translation, self.rotation) = TransformHelpers.convert_pose_inverse_transform(self.odom_to_map.pose)

    def broadcast_last_transform(self):
        """ Make sure that we are always broadcasting the last map to odom transformation.
            This is necessary so things like move_base can work properly. """
        if not (hasattr(self, 'translation') and hasattr(self, 'rotation')):
            return
        self.tf_broadcaster.sendTransform(self.translation, self.rotation, rospy.get_rostime(), self.odom_frame,
                                          self.map_frame)

    def filter_laser(self, ranges):
        """ Takes the message from a laser scan as an array and returns a dictionary of angle:distance pairs"""
        valid_ranges = {}
        for i in range(len(ranges)):
            if ranges[i] > 0.0 and ranges[i] < 3.5:
                valid_ranges[i] = ranges[i]
        return valid_ranges


if __name__ == '__main__':
    n = ParticleFilter()
    r = rospy.Rate(5)

    while not (rospy.is_shutdown()):
        # in the main loop all we do is continuously broadcast the latest map to odom transform
        n.broadcast_last_transform()
        r.sleep()