# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# PLATEFO
# Algorithm to calculate plate forces from tectonic reconstructions
# PlateForces object
# Thomas Schouten and Edward Clennett, 2023
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

# Import libraries
# Standard libraries
import os
import multiprocessing
from typing import List, Optional
import itertools
from copy import deepcopy

# Third-party libraries
import numpy as _numpy
import matplotlib.pyplot as plt
import gplately
from gplately import pygplates
import cartopy.crs as ccrs
import cmcrameri as cmc

# Local libraries
import setup
import functions_main

# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# PLATE FORCES OBJECT
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

class PlateForces():
    def __init__(
            self,
            reconstruction_name: str, 
            reconstruction_times: List[int] or _numpy.array, 
            cases_file: str, 
            cases_sheet: str = "Sheet1", 
            files_dir: str = None, 
        ):
        """
        PlateForces object.

        This object can be instantiated in two ways: either by loading previously stored files, or by generating new files.

        :param reconstruction_name:     Name of the plate reconstruction.
        :param reconstruction_times:    List or array of reconstruction times.
        :param cases_file:              Path to the file containing cases data.
        :param cases_sheet:             Sheet name in the cases file (default is "Sheet1").
        :param files_dir:               Directory for storing/loading files (default is None).
        """
        # Let the user know you're busy
        print("Setting up PlateForces object...")

        # Set files directory
        self.dir_path = os.path.join(os.getcwd(), files_dir)
        if not os.path.exists(self.dir_path):
            os.makedirs(self.dir_path)

        # Store reconstruction name and valid reconstruction times
        self.name = reconstruction_name
        self.times = _numpy.array(reconstruction_times)

        # Download reconstruction files from gplately DataServer
        print("Setting up plate reconstruction...")
        gdownload = gplately.DataServer(reconstruction_name)
        self.rotations, self.topologies, self.polygons = gdownload.get_plate_reconstruction_files()
        self.coastlines, self.continents, self.COBs = gdownload.get_topology_geometries()

        # Create instance of plate reconstruction and resolve topologies for all timesteps
        self.reconstruction = gplately.PlateReconstruction(self.rotations, self.topologies, self.polygons)
        self.resolved_topologies, self.resolved_geometries = {}, {}
        for reconstruction_time in self.times:
            self.resolved_topologies[reconstruction_time] = []
            pygplates.resolve_topologies(
                self.topologies,
                self.rotations, 
                self.resolved_topologies[reconstruction_time], 
                reconstruction_time, 
                anchor_plate_id=0)
            self.resolved_geometries[reconstruction_time] = setup.get_topology_geometries(
                self.reconstruction, reconstruction_time, anchor_plateID=0
            )
        print("Plate reconstruction ready!")

        # Store cases and case options
        self.cases, self.options = setup.get_options(cases_file, cases_sheet)

        # Set mechanical parameters and constants
        self.mech = functions_main.set_mech_params()
        self.constants = functions_main.set_constants()

        # Subdivide cases to accelerate computation
        # For loading
        plate_options = ["Minimum plate area"]
        self.plate_cases = setup.process_cases(self.cases, self.options, plate_options)
        slab_options = ["Slab tesselation spacing"]
        self.slab_cases = setup.process_cases(self.cases, self.options, slab_options)
        point_options = ["Grid spacing"]
        self.point_cases = setup.process_cases(self.cases, self.options, point_options)

        # For torque computation
        slab_pull_options = ["Slab pull torque", "Seafloor age profile", "Strain rate", "Interface mixing", "Sample sediment grid", "Active margin sediments", "Randomise trench orientation", "Randomise slab age"]
        self.slab_pull_cases = setup.process_cases(self.cases, self.options, slab_pull_options)
        slab_bend_options = ["Slab bend torque", "Seafloor age profile"]
        self.slab_bend_cases = setup.process_cases(self.cases, self.options, slab_bend_options)
        interface_shear_options = ["Interface shear torque", "Sample sediment grid", "Active margin sediments", "Strain rate", "Interface mixing"]
        self.interface_shear_cases = setup.process_cases(self.cases, self.options, interface_shear_options)
        gpe_options = ["Continental crust", "Seafloor age profile"]
        self.gpe_cases = setup.process_cases(self.cases, self.options, gpe_options)
        mantle_drag_options = ["Reconstructed motions"]
        self.mantle_drag_cases = setup.process_cases(self.cases, self.options, mantle_drag_options)

        # Load or initialise dictionaries with DataFrames for plates, slabs and points
        self.plates = {}
        self.slabs = {}
        self.points = {}
        self.seafloor = {}

        # Load or initialise plates
        self.plates = setup.load_data(
            self.plates,
            self.reconstruction,
            self.name,
            self.times,
            "Plates",
            self.cases,
            self.options,
            self.plate_cases,
            files_dir,
            resolved_topologies = self.resolved_topologies,
            resolved_geometries = self.resolved_geometries
        )

        # Load or initialise slabs
        self.slabs = setup.load_data(
            self.slabs,
            self.reconstruction,
            self.name,
            self.times,
            "Slabs",
            self.cases,
            self.options,
            self.slab_cases,
            files_dir,
            plates = self.plates,
            resolved_geometries = self.resolved_geometries
        )

        # Load or initialise points
        self.points = setup.load_data(
            self.points,
            self.reconstruction,
            self.name,
            self.times,
            "Points",
            self.cases,
            self.options,
            self.point_cases,
            files_dir,
            plates = self.plates,
            resolved_geometries = self.resolved_geometries
        )

        # Load or initialise seafloor
        self.seafloor = setup.load_data(
            self.seafloor,
            self.reconstruction,
            self.name,
            self.times,
            "Seafloor",
            self.cases,
            self.options,
            self.point_cases,
            files_dir,
        )

        # Set sampling flags to False:
        self.sampled_points = False
        self.sampled_upper_plates = False
        self.sampled_slabs = False
        self.optimised_torques = False

        # Initialise dictionaries to store calibration parameters
        self.residual_torque = {}; self.residual_torque_normalised = {}
        self.driving_torque = {};  self.driving_torque_normalised = {}
        self.opt_sp_const = {}; self.opt_visc = {}
        self.opt_i = {}; self.opt_j = {}

        # Initialise dictionary to contain misfit
        self.misfit = {case: {reconstruction_time: {} for reconstruction_time in self.times} for case in self.cases}

        print("PlateForces object successfully instantiated!")

# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# SAMPLING GRIDS 
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def sample_slabs(self):
        """
        Samples seafloor age (and optionally, sediment thickness) the lower plate along subduction zones
        The results are stored in the `slabs` DataFrame, specifically in the `lower_plate_age`, `sediment_thickness`, and `lower_plate_thickness` fields for each case and reconstruction time.
        """
        # Check options for slabs
        for reconstruction_time in self.times:
            print(f"Sampling slabs at {reconstruction_time} Ma")
            # Select cases
            for key, entries in self.slab_pull_cases.items():
                # Select dictionaries
                this_seafloor = self.seafloor[reconstruction_time][key]

                if self.options[key]["Slab pull torque"] or self.options[key]["Slab bend torque"]:
                    # Sample age and sediment thickness of lower plate from seafloor
                    self.slabs[reconstruction_time][key]["lower_plate_age"], self.slabs[reconstruction_time][key]["sediment_thickness"] = functions_main.sample_slabs_from_seafloor(
                        self.slabs[reconstruction_time][key].lat, 
                        self.slabs[reconstruction_time][key].lon,
                        self.slabs[reconstruction_time][key].trench_normal_azimuth,
                        this_seafloor, 
                        self.options[key],
                        "lower plate",
                        sediment_thickness=self.slabs[reconstruction_time][key].sediment_thickness,
                        continental_arc=self.slabs[reconstruction_time][key].continental_arc,
                    )

                    # Calculate lower plate thickness
                    self.slabs[reconstruction_time][key]["lower_plate_thickness"], crust_thickness, water_depth = functions_main.compute_thicknesses(
                        self.slabs[reconstruction_time][key].lower_plate_age,
                        self.options[key],
                        crust = False, 
                        water = False
                    )
                
                    for entry in entries[1:]:
                        self.slabs[reconstruction_time][entry]["lower_plate_age"] = self.slabs[reconstruction_time][key]["lower_plate_age"]
                        self.slabs[reconstruction_time][entry]["sediment_thickness"] = self.slabs[reconstruction_time][key]["sediment_thickness"]
                        self.slabs[reconstruction_time][entry]["lower_plate_thickness"] = self.slabs[reconstruction_time][key]["lower_plate_thickness"]

                    # these_slabs["left_index"], these_slabs["right_index"] = functions_main.find_nearest_points(these_slabs["lat"], these_slabs["lon"], self.constants, max_distance_km=250)

        self.sampled_slabs = True

    def sample_upper_plate(self):
        """
        Samples seafloor age the upper plate along subduction zones
        The results are stored in the `slabs` DataFrame, specifically in the `upper_plate_age`, `upper_plate_thickness` fields for each case and reconstruction time.
        """
        # Loop through valid times    
        for reconstruction_time in self.times:
            print(f"Sampling overriding plate at {reconstruction_time} Ma")
            # Select cases
            for key, entries in self.slab_pull_cases.items():
                # Select dictionaries
                this_seafloor = self.seafloor[reconstruction_time][key]

                # Sample age and arc type of upper plate from seafloor
                self.slabs[reconstruction_time][key]["upper_plate_age"], self.slabs[reconstruction_time][key]["continental_arc"] = functions_main.sample_slabs_from_seafloor(
                    self.slabs[reconstruction_time][key].lat, 
                    self.slabs[reconstruction_time][key].lon,
                    self.slabs[reconstruction_time][key].trench_normal_azimuth,  
                    this_seafloor,
                    self.options[key],
                    "upper plate"
                )

                for entry in entries[1:]:
                    self.slabs[reconstruction_time][entry]["upper_plate_age"] = self.slabs[reconstruction_time][key]["upper_plate_age"]
                    self.slabs[reconstruction_time][entry]["continental_arc"] = self.slabs[reconstruction_time][key]["continental_arc"]
        
        self.sampled_upper_plates = True

    def sample_points(self):
        """
        Samples seafloor age at points
        The results are stored in the `points` DataFrame, specifically in the `seafloor_age` field for each case and reconstruction time.
        """
        # Loop through valid times
        for reconstruction_time in self.times:
            print(f"Sampling points at {reconstruction_time} Ma")
            for key, entries in self.gpe_cases.items():
                # Select dictionaries
                this_seafloor = self.seafloor[reconstruction_time][key]
                
                self.points[reconstruction_time][key]["seafloor_age"] = functions_main.sample_ages(self.points[reconstruction_time][key].lat, self.points[reconstruction_time][key].lon, this_seafloor["seafloor_age"])
                for entry in entries[1:]:
                    self.points[reconstruction_time][entry]["seafloor_age"] = self.points[reconstruction_time][key]["seafloor_age"]

        self.sampled_points = True

    def sample_all(self):
        """
        Samples all relevant data from the seafloor to perform torque computation
        """
        self.sample_slabs()
        self.sample_upper_plate()
        self.sample_points()

# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# COMPUTING TORQUES
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def compute_torques(self):
        """
        Computes torques 
        """
        # Check if upper plates have been sampled already
        if self.sampled_upper_plates == False:
            self.sample_upper_plate()

        # Check if slabs have been sampled already
        if self.sampled_slabs == False:
            self.sample_slabs()

        # Check if points have been sampled
        if self.sampled_points == False:
            self.sample_points()
        
        # Loop through reconstruction times
        for i, reconstruction_time in enumerate(self.times):
            print(f"Computing torques at {reconstruction_time} Ma")

            #---------------------#
            #   DRIVING TORQUES   #
            #---------------------#
            
            # Loop through slab pull cases
            for key, entries in self.slab_pull_cases.items():

                # Calculate slab pull torque
                if self.options[key]["Slab pull torque"]:
                    these_slabs = self.slabs[reconstruction_time][key]
                    these_plates = self.plates[reconstruction_time][key]
                    these_slabs = functions_main.compute_slab_pull_force(these_slabs, self.options[key], self.mech)

                    these_plates = functions_main.compute_torque_on_plates(
                        these_plates, 
                        these_slabs.lat, 
                        these_slabs.lon, 
                        these_slabs.lower_plateID, 
                        these_slabs.slab_pull_force_lat, 
                        these_slabs.slab_pull_force_lon,
                        these_slabs.trench_segment_length,
                        1,
                        self.constants,
                        torque_variable="slab_pull_torque"
                    )

                    # Overwrite DataFrames
                    self.plates[reconstruction_time][key] = these_plates
                    self.slabs[reconstruction_time][key] = these_slabs

                    # Delete temporary DataFrames
                    del these_slabs, these_plates

                    # Copy DataFrames
                    [[self.slabs[reconstruction_time][entry].update(
                        {"slab_pull_force_" + coord: self.slabs[reconstruction_time][key]["slab_pull_force_" + coord]}
                    ) for coord in ["lat", "lon", "mag"]] for entry in entries[1:]]
                    [[self.plates[reconstruction_time][entry].update(
                        {"slab_pull_force_" + coord: self.plates[reconstruction_time][key]["slab_pull_force_" + coord]}
                    ) for coord in ["lat", "lon", "mag"]] for entry in entries[1:]]
                    [[self.plates[reconstruction_time][entry].update(
                        {"slab_pull_torque_" + axis: self.plates[reconstruction_time][key]["slab_pull_torque_" + axis]}
                    ) for axis in ["x", "y", "z", "mag"]] for entry in entries[1:]]

            # Loop through gpe cases
            for key, entries in self.gpe_cases.items():
                # Calculate GPE torque
                if self.options[key]["GPE torque"]:
                    these_points = self.points[reconstruction_time][key]
                    these_plates = self.plates[reconstruction_time][key]
                    
                    # Select dictionaries
                    this_seafloor = self.seafloor[reconstruction_time][key]
                    these_points = functions_main.compute_GPE_force(these_points, this_seafloor, self.options[key], self.mech)
                    these_plates = functions_main.compute_torque_on_plates(
                        these_plates, 
                        these_points.lat, 
                        these_points.lon, 
                        these_points.plateID, 
                        these_points.GPE_force_lat, 
                        these_points.GPE_force_lon,
                        these_points.segment_length_lat, 
                        these_points.segment_length_lon,
                        self.constants,
                        torque_variable="GPE_torque"
                    )

                    # Overwrite DataFrames
                    self.plates[reconstruction_time][key] = these_plates
                    self.points[reconstruction_time][key] = these_points

                    # Delete temporary DataFrames
                    del these_points, these_plates

                # Copy DataFrames
                [[self.points[reconstruction_time][entry].update(
                    {"GPE_force_" + coord: self.points[reconstruction_time][key]["GPE_force_" + coord]}
                ) for coord in ["lat", "lon", "mag"]] for entry in entries[1:]]
                [[self.plates[reconstruction_time][entry].update(
                    {"GPE_force_" + coord: self.plates[reconstruction_time][key]["GPE_force_" + coord]}
                ) for coord in ["lat", "lon", "mag"]] for entry in entries[1:]]
                [[self.plates[reconstruction_time][entry].update(
                    {"GPE_torque_" + axis: self.plates[reconstruction_time][key]["GPE_torque_" + axis]}
                ) for axis in ["x", "y", "z", "mag"]] for entry in entries[1:]]

            #-----------------------#
            #   RESISTIVE TORQUES   #
            #-----------------------#
            
            # Loop through interface shear cases
            for key, entries in self.interface_shear_cases.items():
                # Calculate interface shear torque
                if self.options[key]["Interface shear torque"]:
                    these_slabs = self.slabs[reconstruction_time][key]
                    these_plates = self.plates[reconstruction_time][key]
                    these_slabs = functions_main.compute_interface_shear_force(these_slabs, self.options[key], self.mech, self.constants)
                    these_plates = functions_main.compute_torque_on_plates(
                        these_plates, 
                        these_slabs.lat, 
                        these_slabs.lon, 
                        these_slabs.lower_plateID, 
                        these_slabs.interface_shear_force_lat, 
                        these_slabs.interface_shear_force_lon,
                        these_slabs.trench_segment_length,
                        1,
                        self.constants,
                        torque_variable="interface_shear_torque"
                    )

                    # Overwrite DataFrames
                    self.plates[reconstruction_time][key] = these_plates
                    self.slabs[reconstruction_time][key] = these_slabs

                    # Delete temporary DataFrames
                    del these_slabs, these_plates
                
                # Copy DataFrames
                [[self.slabs[reconstruction_time][entry].update(
                    {"interface_shear_force_" + coord: self.slabs[reconstruction_time][key]["interface_shear_force_" + coord]}
                ) for coord in ["lat", "lon", "mag"]] for entry in entries[1:]]
                [[self.plates[reconstruction_time][entry].update(
                    {"interface_shear_force_" + coord: self.plates[reconstruction_time][key]["interface_shear_force_" + coord]}
                ) for coord in ["lat", "lon", "mag"]] for entry in entries[1:]] 
                [[self.plates[reconstruction_time][entry].update(
                    {"interface_shear_torque_" + axis: self.plates[reconstruction_time][key]["interface_shear_torque_" + axis]}
                ) for axis in ["x", "y", "z", "mag"]] for entry in entries[1:]] 

            # Loop through slab bend cases
            for key, entries in self.slab_bend_cases.items():
                # Calculate slab bending torque
                if self.options[key]["Slab bend torque"]:
                    these_slabs = self.slabs[reconstruction_time][key].copy()
                    these_plates = self.plates[reconstruction_time][key].copy()
                    these_slabs = functions_main.compute_slab_bend_force(these_slabs, self.options[key], self.mech, self.constants)
                    these_plates = functions_main.compute_torque_on_plates(
                        these_plates, 
                        these_slabs.lat, 
                        these_slabs.lon, 
                        these_slabs.lower_plateID, 
                        these_slabs.slab_bend_force_lat, 
                        these_slabs.slab_bend_force_lon,
                        these_slabs.trench_segment_length,
                        1,
                        self.constants,
                        torque_variable="slab_bend_torque"
                    )

                    # Overwrite DataFrames
                    self.plates[reconstruction_time][key] = these_plates
                    self.slabs[reconstruction_time][key] = these_slabs

                    # Delete temporary DataFrames
                    del these_slabs, these_points, these_plates
                    
                # Copy DataFrames
                [self.slabs[reconstruction_time][entry].update(
                    {"slab_bend_force_" + coord: self.slabs[reconstruction_time][key]["slab_bend_force_" + coord]}
                ) for coord in ["lat", "lon", "mag"] for entry in entries[1:]]
                [self.plates[reconstruction_time][entry].update(
                    {"slab_bend_force_" + coord: self.plates[reconstruction_time][key]["slab_bend_force_" + coord]}
                ) for coord in ["lat", "lon", "mag"] for entry in entries[1:]]
                [self.plates[reconstruction_time][entry].update(
                    {"mantle_drag_torque_" + axis: self.plates[reconstruction_time][key]["mantle_drag_torque_" + axis]}
                ) for axis in ["x", "y", "z", "mag"] for entry in entries[1:]]
                    
            # Loop through mantle drag cases
            for key, entries in self.mantle_drag_cases.items():
                if self.options[key]["Reconstructed motions"]:
                    # Calculate Mantle drag torque
                    if self.options[key]["Mantle drag torque"]:
                        # Copy slabs, points and plates
                        these_slabs = self.slabs[reconstruction_time][key].copy()
                        these_points = self.points[reconstruction_time][key].copy()
                        these_plates = self.plates[reconstruction_time][key].copy()

                        # Calculate mantle drag force
                        these_plates, these_points, these_slabs = functions_main.compute_mantle_drag_force(these_plates, these_points, these_slabs, self.options[key], self.mech, self.constants)
                        
                        # Calculate mantle drag torque
                        these_plates = functions_main.compute_torque_on_plates(
                            these_plates, 
                            these_points.lat, 
                            these_points.lon, 
                            these_points.plateID, 
                            these_points.mantle_drag_force_lat, 
                            these_points.mantle_drag_force_lon,
                            these_points.segment_length_lat,
                            these_points.segment_length_lon,
                            self.constants,
                            torque_variable="mantle_drag_torque"
                        )

                    # Overwrite DataFrames
                    self.plates[reconstruction_time][key] = these_plates
                    self.points[reconstruction_time][key] = these_points
                    self.slabs[reconstruction_time][key] = these_slabs

                    # Delete temporary DataFrames
                    del these_slabs, these_points, these_plates

                # Enter mantle drag torque in other cases
                [self.points[reconstruction_time][entry].update(
                    {"mantle_drag_force_" + coord: self.points[reconstruction_time][key]["mantle_drag_force_" + coord]}
                ) for coord in ["lat", "lon", "mag"] for entry in entries[1:]]
                [self.plates[reconstruction_time][entry].update(
                    {"mantle_drag_force_" + coord: self.plates[reconstruction_time][key]["mantle_drag_force_" + coord]}
                ) for coord in ["lat", "lon", "mag"] for entry in entries[1:]]
                [self.plates[reconstruction_time][entry].update(
                    {"mantle_drag_torque_" + axis: self.plates[reconstruction_time][key]["mantle_drag_torque_" + axis]}
                ) for axis in ["x", "y", "z", "mag"] for entry in entries[1:]]

            # Loop through all cases
            for case in self.cases:
                if not self.options[case]["Reconstructed motions"]:
                    print(case)
                    if self.options[case]["Mantle drag torque"]:
                        # Select slabs, points and plates
                        these_slabs = self.slabs[reconstruction_time][case].copy()
                        these_points = self.points[reconstruction_time][case].copy()
                        these_plates = self.plates[reconstruction_time][case].copy()

                        # Optimise slab pull force
                        [these_plates.update({"slab_pull_torque_opt_" + axis: these_plates["slab_pull_torque_" + axis] * self.options[case]["Slab pull constant"]}) for axis in ["x", "y", "z"]]

                        # Initialise starting old_plates, old_points, old_slabs by copying these_plates, these_points, these_slabs
                        old_plates = these_plates.copy(); old_points = these_points.copy(); old_slabs = these_slabs.copy()

                        # Delete these_slabs, these_points, these_plates
                        del these_slabs, these_points, these_plates

                        for i in range(100):
                            # Delete new DataFrames
                            if i != 0:
                                del new_slabs, new_points, new_plates
                            else:
                                old_slabs["v_convergence_mag"] = 0

                            print(_numpy.mean(old_slabs["v_convergence_mag"].values))
                            # Compute interface shear force
                            if self.options[case]["Interface shear torque"]:
                                new_slabs = functions_main.compute_interface_shear_force(old_slabs, self.options[case], self.mech, self.constants)
                            else:
                                new_slabs = old_slabs.copy()

                            # Compute interface shear torque
                            new_plates = functions_main.compute_torque_on_plates(
                                old_plates,
                                new_slabs.lat,
                                new_slabs.lon,
                                new_slabs.lower_plateID,
                                new_slabs.interface_shear_force_lat,
                                new_slabs.interface_shear_force_lon,
                                new_slabs.trench_segment_length,
                                1,
                                self.constants,
                                torque_variable="interface_shear_torque"
                            )

                            # Compute mantle drag force
                            new_plates, new_points, new_slabs = functions_main.compute_mantle_drag_force(old_plates, old_points, new_slabs, self.options[case], self.mech, self.constants)

                            # Compute mantle drag torque
                            new_plates = functions_main.compute_torque_on_plates(
                                new_plates, 
                                new_points.lat, 
                                new_points.lon, 
                                new_points.plateID, 
                                new_points.mantle_drag_force_lat, 
                                new_points.mantle_drag_force_lon,
                                new_points.segment_length_lat,
                                new_points.segment_length_lon,
                                self.constants,
                                torque_variable="mantle_drag_torque"
                            )

                            # Calculate convergence rates
                            v_convergence_lat = new_slabs["v_lower_plate_lat"].values; v_convergence_lon = new_slabs["v_lower_plate_lon"].values

                            if not self.options[case]["Mantle stationary trenches"]:
                                v_convergence_lat -= new_slabs["v_upper_plate_lat"].values; v_convergence_lon -= new_slabs["v_upper_plate_lon"].values

                            v_convergence_mag = _numpy.sqrt(v_convergence_lat**2 + v_convergence_lon**2)

                            # Check convergence rates
                            if _numpy.max(abs(v_convergence_mag - old_slabs["v_convergence_mag"].values)) < 1e-2: # and _numpy.max(v_convergence_mag) < 25:
                                print(f"Convergence rates converged after {i} iterations")
                                break
                            else:
                                # Assign new values to latest slabs DataFrame
                                new_slabs["v_convergence_lat"], new_slabs["v_convergence_lon"] = functions_main.mag_azi2lat_lon(v_convergence_mag, new_slabs.trench_normal_azimuth); new_slabs["v_convergence_mag"] = v_convergence_mag
                                
                                # Delecte old DataFrames
                                del old_plates, old_points, old_slabs
                                
                                # Overwrite DataFrames
                                old_plates = new_plates.copy(); old_points = new_points.copy(); old_slabs = new_slabs.copy()

                        # Overwrite DataFrames
                        self.plates[reconstruction_time][case] = new_plates.copy()
                        self.points[reconstruction_time][case] = new_points.copy()
                        self.slabs[reconstruction_time][case] = new_slabs.copy()

                        # Delete temporary DataFrames
                        del new_plates, new_points, new_slabs

# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# OPTIMISATION 
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def minimise_residual_torque(
            self,
            opt_time,
            opt_case,
            plates_of_interest=None,
            grid_size=500,
            visc_range=[1e19, 5e20],
            plot=True,
            weight_by_area=True,
            minimum_plate_area=0
        ):
        """
        Function to find optimised coefficients to match plate motions using a grid search

        :param opt_time:                reconstruction time to optimise
        :type opt_time:                 int
        :param opt_case:                case to optimise
        :type opt_case:                 str
        :param plates_of_interest:      plate IDs to include in optimisation
        :type plates_of_interest:       list of integers or None
        :param grid_size:               size of the grid to find optimal viscosity and slab pull coefficient
        :type grid_size:                int
        :param plot:                    whether or not to plot the grid
        :type plot:                     boolean
        :param weight_by_area:          whether or not to weight the residual torque by plate area
        :type weight_by_area:           boolean

        :return:                        None
        """
        # Generate grid of viscosities and slab pull coefficients
        viscs = _numpy.linspace(visc_range[0],visc_range[1],grid_size)
        sp_consts = _numpy.linspace(1e-4,1,grid_size)
        visc_grid, sp_const_grid = _numpy.meshgrid(viscs, sp_consts)
        ones_grid = _numpy.ones_like(visc_grid)

        # Filter plates
        selected_plates = self.plates[opt_time][opt_case].copy()
        if plates_of_interest:
            selected_plates = selected_plates[selected_plates["plateID"].isin(plates_of_interest)]
            selected_plates = selected_plates.reset_index(drop=True)
        else:
            plates_of_interest = selected_plates["plateID"]

        # Filter plates by minimum area
        selected_plates = selected_plates[selected_plates["area"] > minimum_plate_area]
        selected_plates = selected_plates.reset_index(drop=True)
        plates_of_interest = selected_plates["plateID"]

        # Get total area
        total_area = selected_plates["area"].sum()

        # Initialise dictionaries and arrays to store driving and residual torques
        if opt_time not in self.driving_torque:
            self.driving_torque[opt_time] = {}
        if opt_time not in self.driving_torque_normalised:
            self.driving_torque_normalised[opt_time] = {}
        if opt_time not in self.residual_torque:
            self.residual_torque[opt_time] = {}
        if opt_time not in self.residual_torque_normalised:
            self.residual_torque_normalised[opt_time] = {}
            
        self.driving_torque[opt_time][opt_case] = _numpy.zeros_like(sp_const_grid); self.driving_torque_normalised[opt_time][opt_case] = _numpy.zeros_like(sp_const_grid)
        self.residual_torque[opt_time][opt_case] = _numpy.zeros_like(sp_const_grid); self.residual_torque_normalised[opt_time][opt_case] = _numpy.zeros_like(sp_const_grid)

        # Initialise dictionaries to store optimal coefficients
        if opt_time not in self.opt_i:
            self.opt_i[opt_time] = {}
        if opt_time not in self.opt_j:
            self.opt_j[opt_time] = {}
        if opt_time not in self.opt_sp_const:
            self.opt_sp_const[opt_time] = {}
        if opt_time not in self.opt_visc:
            self.opt_visc[opt_time] = {}

        # Get torques
        for k, _ in enumerate(plates_of_interest):
            residual_x = _numpy.zeros_like(sp_const_grid); residual_y = _numpy.zeros_like(sp_const_grid); residual_z = _numpy.zeros_like(sp_const_grid)
            if self.options[opt_case]["Slab pull torque"] and "slab_pull_torque_x" in selected_plates.columns:
                residual_x -= selected_plates.slab_pull_torque_x.iloc[k] * sp_const_grid
                residual_y -= selected_plates.slab_pull_torque_y.iloc[k] * sp_const_grid
                residual_z -= selected_plates.slab_pull_torque_z.iloc[k] * sp_const_grid

            # Add GPE torque
            if self.options[opt_case]["GPE torque"] and "GPE_torque_x" in selected_plates.columns:
                residual_x -= selected_plates.GPE_torque_x.iloc[k] * ones_grid
                residual_y -= selected_plates.GPE_torque_y.iloc[k] * ones_grid
                residual_z -= selected_plates.GPE_torque_z.iloc[k] * ones_grid
            
            # Compute magnitude of driving torque
            if weight_by_area:
                self.driving_torque[opt_time][opt_case] += _numpy.sqrt(residual_x**2 + residual_y**2 + residual_z**2) * selected_plates.area.iloc[k] / total_area
            else:
                self.driving_torque[opt_time][opt_case] += _numpy.sqrt(residual_x**2 + residual_y**2 + residual_z**2) / selected_plates.area.iloc[k]

            # Add interface shear torque
            if self.options[opt_case]["Interface shear torque"] and "interface_shear_torque_x" in selected_plates.columns:
                residual_x -= selected_plates.interface_shear_torque_x.iloc[k] * ones_grid
                residual_y -= selected_plates.interface_shear_torque_y.iloc[k] * ones_grid
                residual_z -= selected_plates.interface_shear_torque_z.iloc[k] * ones_grid

            # Add slab bend torque
            if self.options[opt_case]["Slab bend torque"] and "slab_bend_torque_x" in selected_plates.columns:
                residual_x -= selected_plates.slab_bend_torque_x.iloc[k] * ones_grid
                residual_y -= selected_plates.slab_bend_torque_y.iloc[k] * ones_grid
                residual_z -= selected_plates.slab_bend_torque_z.iloc[k] * ones_grid

            # Add mantle drag torque
            if self.options[opt_case]["Mantle drag torque"] and "mantle_drag_torque_x" in selected_plates.columns:
                residual_x -= selected_plates.mantle_drag_torque_x.iloc[k] * visc_grid / self.mech.La
                residual_y -= selected_plates.mantle_drag_torque_y.iloc[k] * visc_grid / self.mech.La
                residual_z -= selected_plates.mantle_drag_torque_z.iloc[k] * visc_grid / self.mech.La

            # Compute magnitude of residual
            if weight_by_area:
                self.residual_torque[opt_time][opt_case] += _numpy.sqrt(residual_x**2 + residual_y**2 + residual_z**2) * selected_plates.area.iloc[k] / total_area
            else:
                self.residual_torque[opt_time][opt_case] += _numpy.sqrt(residual_x**2 + residual_y**2 + residual_z**2) / selected_plates.area.iloc[k]
    
            # Divide residual by driving torque
            self.residual_torque_normalised[opt_time][opt_case] = _numpy.log10(self.residual_torque[opt_time][opt_case] / self.driving_torque[opt_time][opt_case])

        # Find the indices of the minimum value directly using _numpy.argmin
        self.opt_i[opt_time][opt_case], self.opt_j[opt_time][opt_case] = _numpy.unravel_index(_numpy.argmin(self.residual_torque_normalised[opt_time][opt_case]), self.residual_torque_normalised[opt_time][opt_case].shape)
        self.opt_visc[opt_time][opt_case] = visc_grid[self.opt_i[opt_time][opt_case], self.opt_j[opt_time][opt_case]]
        self.opt_sp_const[opt_time][opt_case] = sp_const_grid[self.opt_i[opt_time][opt_case], self.opt_j[opt_time][opt_case]]

        # Plot
        if plot == True:
            fig, ax = plt.subplots(figsize=(15*self.constants.cm2in, 12*self.constants.cm2in))
            im = ax.imshow(self.residual_torque_normalised[opt_time][opt_case], cmap="cmc.lapaz_r", vmin=-2, vmax=2)
            ax.set_yticks(_numpy.linspace(0, grid_size - 1, 5))
            ax.set_xticks(_numpy.linspace(0, grid_size - 1, 5))
            ax.set_xticklabels(["{:.2e}".format(visc) for visc in _numpy.linspace(visc_range[0], visc_range[1], 5)])
            ax.set_yticklabels(["{:.2f}".format(sp_const) for sp_const in _numpy.linspace(sp_consts.min(), sp_consts.max(), 5)])
            ax.set_xlabel("Mantle viscosity [Pa s]")
            ax.set_ylabel("Slab pull reduction factor")
            ax.scatter(self.opt_j[opt_time][opt_case], self.opt_i[opt_time][opt_case], marker="*", facecolor="none", edgecolor="k", s=30)  # Adjust the marker style and size as needed
            fig.colorbar(im, label = "Log(residual torque/driving torque)")
            plt.show()

        # Print results
        print(f"Optimal coefficients for ", ", ".join(selected_plates.name.astype(str)), " plate(s), (PlateIDs: ", ", ".join(selected_plates.plateID.astype(str)), ")")
        print("Minimum residual torque: {:.2%} of driving torque".format(10**(_numpy.amin(self.residual_torque_normalised[opt_time][opt_case]))))
        print("Optimum viscosity [Pa s]: {:.2e}".format(self.opt_visc[opt_time][opt_case]))
        print("Optimum Drag Coefficient [Pa s/m]: {:.2e}".format(self.opt_visc[opt_time][opt_case] / self.mech.La))
        print("Optimum Slab Pull constant: {:.2%}".format(self.opt_sp_const[opt_time][opt_case]))

        return self.opt_sp_const[opt_time][opt_case], self.opt_visc[opt_time][opt_case], self.residual_torque_normalised[opt_time][opt_case]
    
    def minimise_residual_torque_v2(self, opt_time, opt_case, plates_of_interest=None, grid_size=500, visc_range=[1e19, 5e20], plot=True, weight_by_area=True):
        """
        Function to find optimised coefficients to match plate motions using a grid search

        :param opt_time:                reconstruction time to optimise
        :type opt_time:                 int
        :param opt_case:                case to optimise
        :type opt_case:                 str
        :param plates_of_interest:      plate IDs to include in optimisation
        :type plates_of_interest:       list of integers or None
        :param grid_size:               size of the grid to find optimal viscosity and slab pull coefficient
        :type grid_size:                int
        :param plot:                    whether or not to plot the grid
        :type plot:                     boolean
        :param weight_by_area:          whether or not to weight the residual torque by plate area
        :type weight_by_area:           boolean
        """
        # Define ranges
        viscs = _numpy.linspace(visc_range[0],visc_range[1],grid_size)
        int_consts = _numpy.linspace(1e-4,1,grid_size)
        slab_consts = _numpy.linspace(1e-4,1,grid_size)

        # Generate grid
        visc_grid = _numpy.repeat(viscs[_numpy.newaxis, :], grid_size, axis=0)
        int_const_grid = _numpy.repeat(int_consts[:, _numpy.newaxis], grid_size, axis=1)
        slab_const_grid = _numpy.repeat(slab_consts[:, _numpy.newaxis], grid_size, axis=1)

        visc_grid_3d, int_const_grid_3d, slab_const_grid_3d = _numpy.meshgrid(visc_grid, int_const_grid, slab_const_grid, indexing='ij')
        ones_grid = _numpy.ones_like(visc_grid)

        # Filter plates
        selected_plates = self.plates[opt_time][opt_case].copy()
        selected_slabs = self.slabs[opt_time][opt_case].copy()
        if plates_of_interest:
            selected_plates = selected_plates[selected_plates["plateID"].isin(plates_of_interest)]
            selected_plates = selected_plates.reset_index(drop=True)
            selected_slabs = selected_slabs[selected_slabs["lower_plateID"].isin(plates_of_interest)]
            selected_slabs = selected_slabs.reset_index(drop=True)
        else:
            plates_of_interest = selected_plates["plateID"]

        # Get total area
        total_area = selected_plates["area"].sum()

        # Initialise dictionaries and arrays to store driving and residual torques
        if opt_time not in self.driving_torque.keys():
            self.driving_torque[opt_time] = {}
        if opt_time not in self.driving_torque_normalised.keys():
            self.driving_torque_normalised[opt_time] = {}
        if opt_time not in self.residual_torque.keys():
            self.residual_torque[opt_time] = {}
        if opt_time not in self.residual_torque_normalised.keys():
            self.residual_torque_normalised[opt_time] = {}
            
        self.driving_torque[opt_time][opt_case] = _numpy.zeros_like(sp_const_grid); self.driving_torque_normalised[opt_time][opt_case] = _numpy.zeros_like(sp_const_grid)
        self.residual_torque[opt_time][opt_case] = _numpy.zeros_like(sp_const_grid); self.residual_torque_normalised[opt_time][opt_case] = _numpy.zeros_like(sp_const_grid)

        # Initialise dictionaries to store optimal coefficients
        if opt_time not in self.opt_i.keys():
            self.opt_i[opt_time] = {}
        if opt_time not in self.opt_j.keys():
            self.opt_j[opt_time] = {}
        if opt_time not in self.opt_sp_const.keys():
            self.opt_sp_const[opt_time] = {}
        if opt_time not in self.opt_visc.keys():
            self.opt_visc[opt_time] = {}

        # Get torques
        for k, _ in enumerate(plates_of_interest):
            # Calculate slab pull torque
            residual_x = _numpy.zeros_like(sp_const_grid); residual_y = _numpy.zeros_like(sp_const_grid); residual_z = _numpy.zeros_like(sp_const_grid)
            if self.options[opt_case]["Slab pull torque"] and "slab_pull_torque_x" in selected_plates.columns:
                slab_pull_torque_x = _numpy.zeros_like(sp_consts); slab_pull_torque_y = _numpy.zeros_like(sp_consts); slab_pull_torque_z = _numpy.zeros_like(sp_consts)
                for i, (int_const, slab_const) in enumerate(int_consts, slab_consts):
                    sp_const_plates = functions_main.torque_on_plates(
                                selected_plates, 
                                selected_slabs.lat, 
                                selected_slabs.lon, 
                                selected_slabs.lower_plateID, 
                                selected_slabs.slab_pull_force_lat * sp_const * (selected_slabs.sediment_fraction+1), 
                                selected_slabs.slab_pull_force_lon * sp_const * (selected_slabs.sediment_fraction+1),
                                selected_slabs.trench_segment_length,
                                1,
                                self.constants,
                                torque_variable="slab_pull_torque"
                            )
                    slab_pull_torque_x[i] = sp_const_plates.slab_pull_torque_x.iloc[k]
                    slab_pull_torque_y[i] = sp_const_plates.slab_pull_torque_y.iloc[k]
                    slab_pull_torque_z[i] = sp_const_plates.slab_pull_torque_z.iloc[k]
                
                # Expand to grid for vectorised calculation
                slab_pull_torque_x_grid = _numpy.repeat(slab_pull_torque_x[:, _numpy.newaxis], grid_size, axis=1)
                slab_pull_torque_y_grid = _numpy.repeat(slab_pull_torque_y[:, _numpy.newaxis], grid_size, axis=1)
                slab_pull_torque_z_grid = _numpy.repeat(slab_pull_torque_z[:, _numpy.newaxis], grid_size, axis=1)

                # Add to residual
                residual_x -= slab_pull_torque_x_grid
                residual_y -= slab_pull_torque_y_grid
                residual_z -= slab_pull_torque_z_grid

            # Add GPE torque
            if self.options[opt_case]["GPE torque"] and "GPE_torque_x" in selected_plates.columns:
                residual_x -= selected_plates.GPE_torque_x.iloc[k] * ones_grid
                residual_y -= selected_plates.GPE_torque_y.iloc[k] * ones_grid
                residual_z -= selected_plates.GPE_torque_z.iloc[k] * ones_grid
            
            # Compute magnitude of driving torque
            if weight_by_area:
                self.driving_torque[opt_time][opt_case] += _numpy.sqrt(residual_x**2 + residual_y**2 + residual_z**2) * selected_plates.area.iloc[k] / total_area
            else:
                self.driving_torque[opt_time][opt_case] += _numpy.sqrt(residual_x**2 + residual_y**2 + residual_z**2) / selected_plates.area.iloc[k]

            # Add slab bend torque
            if self.options[opt_case]["Slab bend torque"] and "slab_bend_torque_x" in selected_plates.columns:
                residual_x -= selected_plates.slab_bend_torque_x.iloc[k] * ones_grid
                residual_y -= selected_plates.slab_bend_torque_y.iloc[k] * ones_grid
                residual_z -= selected_plates.slab_bend_torque_z.iloc[k] * ones_grid

            # Add mantle drag torque
            if self.options[opt_case]["Mantle drag torque"] and "mantle_drag_torque_x" in selected_plates.columns:
                residual_x -= selected_plates.mantle_drag_torque_x.iloc[k] * visc_grid / self.mech.La
                residual_y -= selected_plates.mantle_drag_torque_y.iloc[k] * visc_grid / self.mech.La
                residual_z -= selected_plates.mantle_drag_torque_z.iloc[k] * visc_grid / self.mech.La

            # Compute magnitude of residual
            if weight_by_area:
                self.residual_torque[opt_time][opt_case] += _numpy.sqrt(residual_x**2 + residual_y**2 + residual_z**2) * selected_plates.area.iloc[k] / total_area
            else:
                self.residual_torque[opt_time][opt_case] += _numpy.sqrt(residual_x**2 + residual_y**2 + residual_z**2) / selected_plates.area.iloc[k]
    
            # Divide residual by driving torque
            self.residual_torque_normalised[opt_time][opt_case] = _numpy.log10(self.residual_torque[opt_time][opt_case] / self.driving_torque[opt_time][opt_case])

        # Find the indices of the minimum value directly using _numpy.argmin
        self.opt_i[opt_time][opt_case], self.opt_j[opt_time][opt_case] = _numpy.unravel_index(_numpy.argmin(self.residual_torque_normalised[opt_time][opt_case]), self.residual_torque_normalised[opt_time][opt_case].shape)
        self.opt_visc[opt_time][opt_case] = visc_grid[self.opt_i[opt_time][opt_case], self.opt_j[opt_time][opt_case]]
        self.opt_sp_const[opt_time][opt_case] = sp_const_grid[self.opt_i[opt_time][opt_case], self.opt_j[opt_time][opt_case]]

        # Plot
        if plot == True:
            fig, ax = plt.subplots(figsize=(15*self.constants.cm2in, 12*self.constants.cm2in))
            im = ax.imshow(self.residual_torque_normalised[opt_time][opt_case], cmap="cmc.lapaz_r", vmin=-1.5, vmax=1.5)
            ax.set_yticks(_numpy.linspace(0, grid_size - 1, 5))
            ax.set_xticks(_numpy.linspace(0, grid_size - 1, 5))
            ax.set_xticklabels(["{:.2e}".format(visc) for visc in _numpy.linspace(visc_range[0], visc_range[1], 5)])
            ax.set_yticklabels(["{:.2f}".format(sp_const) for sp_const in _numpy.linspace(sp_consts.min(), sp_consts.max(), 5)])
            ax.set_xlabel("Mantle viscosity [Pa s]")
            ax.set_ylabel("Slab pull reduction factor")
            ax.scatter(self.opt_j[opt_time][opt_case], self.opt_i[opt_time][opt_case], marker="*", facecolor="none", edgecolor="k", s=30)  # Adjust the marker style and size as needed
            fig.colorbar(im, label = "Log(residual torque/driving torque)")
            plt.show()
        
        print(f"Optimal coefficients for ", ", ".join(selected_plates.name.astype(str)), " plate(s), (PlateIDs: ", ", ".join(selected_plates.plateID.astype(str)), ")")
        print("Minimum residual torque: {:.2%} of driving torque".format(10**(_numpy.amin(self.residual_torque_normalised[opt_time][opt_case]))))
        print("Optimum viscosity [Pa s]: {:.2e}".format(self.opt_visc[opt_time][opt_case]))
        print("Optimum Drag Coefficient [Pa s/m]: {:.2e}".format(self.opt_visc[opt_time][opt_case] / self.mech.La))
        print("Optimum Slab Pull constant: {:.2%}".format(self.opt_sp_const[opt_time][opt_case]))

        return self.opt_sp_const[opt_time][opt_case], self.opt_visc[opt_time][opt_case], self.residual_torque_normalised[opt_time][opt_case]

    def minimise_residual_velocity(self, opt_time, opt_case, plates_of_interest=None, grid_size=100, visc_range=[1e19, 5e20], plot=True, weight_by_area=True):
        """
        Function to find optimised coefficients to match plate motions using a grid search

        :param opt_time:                reconstruction time to optimise
        :type opt_time:                 int
        :param opt_case:                case to optimise
        :type opt_case:                 str
        :param plates_of_interest:      plate IDs to include in optimisation
        :type plates_of_interest:       list of integers or None
        :param grid_size:               size of the grid to find optimal viscosity and slab pull coefficient
        :type grid_size:                int
        :param plot:                    whether or not to plot the grid
        :type plot:                     boolean
        :param weight_by_area:          whether or not to weight the residual torque by plate area
        :type weight_by_area:           boolean
        """
        # Generate grid
        viscs = _numpy.linspace(visc_range[0],visc_range[1],grid_size)
        sp_consts = _numpy.linspace(1e-4,1,grid_size)
        v_plate_residual = _numpy.zeros((grid_size, grid_size))
        v_slab_residual = _numpy.zeros((grid_size, grid_size))
        
        # Filter plates and slabs
        selected_plates = self.plates[opt_time][opt_case].copy()
        selected_slabs = self.slabs[opt_time][opt_case].copy()
        selected_points = self.points[opt_time][opt_case].copy()
        if plates_of_interest:
            selected_plates = selected_plates[selected_plates["plateID"].isin(plates_of_interest)]
            selected_plates = selected_plates.reset_index(drop=True)
            selected_slabs = selected_slabs[selected_slabs["lower_plateID"].isin(plates_of_interest)]
            selected_slabs = selected_slabs.reset_index(drop=True)
            selected_points = selected_points[selected_points["plateID"].isin(plates_of_interest)]
            selected_points = selected_points.reset_index(drop=True)
            selected_options = self.options[opt_case].copy()
        else:
            plates_of_interest = selected_plates["plateID"]

        selected_options["Reconstructed motions"] = False
        # Loop through plates and slabs and calculate residual velocity
        for i, visc in enumerate(viscs):
            if i % 10 == 0:
                print("Calculating residual velocities for viscosity {:.2e}".format(visc))
            selected_options["Mantle viscosity"] = visc
            for j, sp_const in enumerate(sp_consts):
                selected_options["Slab pull constant"] = sp_const
                # Calculate mantle drag force
                these_plates = selected_plates.copy()
                these_slabs = selected_slabs.copy()
                these_points = selected_points.copy()
                these_plates, these_points = functions_main.mantle_drag_force(these_plates, these_points, these_slabs, selected_options, self.mech, self.constants)
                these_plates = functions_main.torque_on_plates(
                    these_plates, 
                    these_points.lat, 
                    these_points.lon, 
                    these_points.plateID, 
                    these_points.mantle_drag_force_lat, 
                    these_points.mantle_drag_force_lon,
                    these_points.segment_length_lat,
                    these_points.segment_length_lon,
                    self.constants,
                    torque_variable="mantle_drag_torque"
                )

                # Calculate residual of plate velocities
                if weight_by_area:
                    v_plate_residual_lon = ((these_plates.v_synthetic_lon-these_plates.v_absolute_lon) * these_plates.area).sum() / these_plates.area.sum()
                    v_plate_residual_lat = ((these_plates.v_synthetic_lat-these_plates.v_absolute_lat) * these_plates.area).sum() / these_plates.area.sum()
                else:
                    v_plate_residual_lon = _numpy.mean(these_plates.v_synthetic_lon-these_plates.v_absolute_lon)
                    v_plate_residual_lat = _numpy.mean(these_plates.v_synthetic_lat-these_plates.v_absolute_lat)
                v_plate_residual[i,j] = _numpy.sqrt(v_plate_residual_lon**2 + v_plate_residual_lat**2)

                # Calculate residual of slab velocities
                v_slab_residual_lon = ((these_slabs.v_synthetic_lon-these_slabs.v_absolute_lon) * these_slabs.trench_segment_length).sum()  / these_slabs.trench_segment_length.sum()
                v_slab_residual_lat = ((these_slabs.v_synthetic_lat-these_slabs.v_absolute_lat) * these_slabs.trench_segment_length).sum() / these_slabs.trench_segment_length.sum()
                v_slab_residual[i,j] = _numpy.sqrt(v_slab_residual_lon**2 + v_slab_residual_lat**2)

        # Find the indices of the minimum value directly using _numpy.argmin
        opt_plate_i, opt_plate_j = _numpy.unravel_index(_numpy.argmin(v_plate_residual), v_plate_residual.shape)
        opt_plate_visc = viscs[opt_plate_i]
        opt_plate_sp_const = sp_consts[opt_plate_j]

        opt_slab_i, opt_slab_j = _numpy.unravel_index(_numpy.argmin(v_slab_residual), v_slab_residual.shape)
        opt_slab_visc = viscs[opt_slab_i]
        opt_slab_sp_const = sp_consts[opt_slab_j]

        # Plot
        if plot == True:
            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(33*self.constants.cm2in, 12*self.constants.cm2in))
            im1 = ax1.imshow(v_plate_residual, cmap="cmc.davos_r", vmin=0, vmax=10)
            ax1.scatter(opt_plate_j, opt_plate_i, marker="*", facecolor="none", edgecolor="k", s=30)  # Adjust the marker style and size as needed
            fig.colorbar(im1, ax=ax1, label = "Residual velocity magnitude [cm/a]")

            im2 = ax2.imshow(v_slab_residual, cmap="cmc.davos_r", vmin=0, vmax=10)
            ax2.scatter(opt_slab_j, opt_slab_i, marker="*", facecolor="none", edgecolor="k", s=30)  # Adjust the marker style and size as needed
            fig.colorbar(im2, ax=ax2, label = "Residual velocity magnitude [cm/a]")

            for ax in [ax1, ax2]:
                ax.set_yticks(_numpy.linspace(0, grid_size - 1, 5))
                ax.set_xticks(_numpy.linspace(0, grid_size - 1, 5))
                ax.set_xticklabels(["{:.2e}".format(visc) for visc in _numpy.linspace(visc_range[0], visc_range[1], 5)])
                ax.set_yticklabels(["{:.2f}".format(sp_const) for sp_const in _numpy.linspace(sp_consts.min(), sp_consts.max(), 5)])
                ax.set_xlabel("Mantle viscosity [Pa s]")
                ax.set_ylabel("Slab pull reduction factor")

            plt.show()
        
        print(f"Optimal coefficients for ", ", ".join(selected_plates.name.astype(str)), " plate(s), (PlateIDs: ", ", ".join(selected_plates.plateID.astype(str)), ")")
        print("Minimum residual plate velocity: {:.2f} cm/a".format(_numpy.amin(v_plate_residual)))
        print("Optimum viscosity [Pa s]: {:.2e}".format(opt_plate_visc))
        print("Optimum Drag Coefficient [Pa s/m]: {:.2e}".format(opt_plate_visc / 150e3))
        print("Optimum Slab Pull constant: {:.2%}".format(opt_plate_sp_const))    
        print("Minimum residual slab velocity: {:.2f} cm/a".format(_numpy.amin(v_slab_residual)))
        print("Optimum viscosity [Pa s]: {:.2e}".format(opt_slab_visc))
        print("Optimum Drag Coefficient [Pa s/m]: {:.2e}".format(opt_slab_visc / 150e3))
        print("Optimum Slab Pull constant: {:.2%}".format(opt_slab_sp_const))  

        return self.opt_sp_const, self.opt_visc, v_plate_residual, v_slab_residual
    
    def minimise_residual_velocity_v2(self, opt_time, opt_case, plates_of_interest=None, grid_size=10, visc_range=[1e19, 5e20], plot=True, weight_by_area=True, ref_case=None):
        """
        Function to find optimised coefficients to match plate motions using a grid search.

        :param opt_time:                reconstruction time to optimise
        :type opt_time:                 int
        :param opt_case:                case to optimise
        :type opt_case:                 str
        :param plates_of_interest:      plate IDs to include in optimisation
        :type plates_of_interest:       list of integers or None
        :param grid_size:               size of the grid to find optimal viscosity and slab pull coefficient
        :type grid_size:                int
        :param plot:                    whether or not to plot the grid
        :type plot:                     boolean
        :param weight_by_area:          whether or not to weight the residual torque by plate area
        :type weight_by_area:           boolean
        
        :return:                        The optimal slab pull coefficient, the optimal viscosity, the residual plate velocity, and the residual slab velocity.
        :rtype:                         float, float, float, float
        """
        if self.options[opt_case]["Reconstructed motions"]:
            print("Optimisation method designed for synthetic plate velocities only!")
            return
        
        # Get "true" plate velocities
        true_slabs = self.slabs[opt_time][ref_case].copy()

        # Generate grid
        viscs = _numpy.linspace(visc_range[0],visc_range[1],grid_size)
        sp_consts = _numpy.linspace(1e-4,1,grid_size)
        v_upper_plate_residual = _numpy.zeros((grid_size, grid_size))
        v_lower_plate_residual = _numpy.zeros((grid_size, grid_size))
        v_convergence_residual = _numpy.zeros((grid_size, grid_size))

        # Filter plates and slabs
        selected_plates = self.plates[opt_time][opt_case].copy()
        selected_slabs = self.slabs[opt_time][opt_case].copy()
        selected_points = self.points[opt_time][opt_case].copy()

        if plates_of_interest:
            selected_plates = selected_plates[selected_plates["plateID"].isin(plates_of_interest)]
            selected_plates = selected_plates.reset_index(drop=True)
            selected_slabs = selected_slabs[selected_slabs["lower_plateID"].isin(plates_of_interest)]
            selected_slabs = selected_slabs.reset_index(drop=True)
            selected_points = selected_points[selected_points["plateID"].isin(plates_of_interest)]
            selected_points = selected_points.reset_index(drop=True)
            selected_options = self.options[opt_case].copy()
        else:
            plates_of_interest = selected_plates["plateID"]

        # Initialise starting old_plates, old_points, old_slabs by copying these_plates, these_points, these_slabs
        old_plates = selected_plates.copy(); old_points = selected_points.copy(); old_slabs = selected_slabs.copy()

        # Delete these_slabs, these_points, these_plates
        del selected_plates, selected_points, selected_slabs
        
        # Loop through plates and slabs and calculate residual velocity
        for i, visc in enumerate(viscs):
            for j, sp_const in enumerate(sp_consts):
                # Assign current visc and sp_const to options
                selected_options["Mantle viscosity"] = visc
                selected_options["Slab pull constant"] = sp_const

                # Optimise slab pull force
                [old_plates.update({"slab_pull_torque_opt_" + axis: old_plates["slab_pull_torque_" + axis] * selected_options["Slab pull constant"]}) for axis in ["x", "y", "z"]]

                for i in range(100):
                    # Delete new DataFrames
                    if i != 0:
                        del new_slabs, new_points, new_plates
                    else:
                        old_slabs["v_convergence_mag"] = 0

                    print(_numpy.mean(old_slabs["v_convergence_mag"].values))
                    # Compute interface shear force
                    if self.options[opt_case]["Interface shear torque"]:
                        new_slabs = functions_main.interface_shear_force(old_slabs, self.options[opt_case], self.mech, self.constants)
                    else:
                        new_slabs = old_slabs.copy()

                    # Compute interface shear torque
                    new_plates = functions_main.torque_on_plates(
                        old_plates,
                        new_slabs.lat,
                        new_slabs.lon,
                        new_slabs.lower_plateID,
                        new_slabs.interface_shear_force_lat,
                        new_slabs.interface_shear_force_lon,
                        new_slabs.trench_segment_length,
                        1,
                        self.constants,
                        torque_variable="interface_shear_torque"
                    )

                    # Compute mantle drag force
                    new_plates, new_points, new_slabs = functions_main.mantle_drag_force(old_plates, old_points, new_slabs, self.options[opt_case], self.mech, self.constants)

                    # Compute mantle drag torque
                    new_plates = functions_main.torque_on_plates(
                        new_plates, 
                        new_points.lat, 
                        new_points.lon, 
                        new_points.plateID, 
                        new_points.mantle_drag_force_lat, 
                        new_points.mantle_drag_force_lon,
                        new_points.segment_length_lat,
                        new_points.segment_length_lon,
                        self.constants,
                        torque_variable="mantle_drag_torque"
                    )

                    # Calculate convergence rates
                    v_convergence_lat = new_slabs["v_lower_plate_lat"].values - new_slabs["v_upper_plate_lat"].values
                    v_convergence_lon = new_slabs["v_lower_plate_lon"].values - new_slabs["v_upper_plate_lon"].values
                    v_convergence_mag = _numpy.sqrt(v_convergence_lat**2 + v_convergence_lon**2)

                    # Calculate convergence rates
                    v_convergence_lat = new_slabs["v_lower_plate_lat"].values - new_slabs["v_upper_plate_lat"].values
                    v_convergence_lon = new_slabs["v_lower_plate_lon"].values - new_slabs["v_upper_plate_lon"].values
                    v_convergence_mag = _numpy.sqrt(v_convergence_lat**2 + v_convergence_lon**2)

                    # Check convergence rates
                    if _numpy.max(abs(v_convergence_mag - old_slabs["v_convergence_mag"].values)) < 1e-2: # and _numpy.max(v_convergence_mag) < 25:
                        print(f"Convergence rates converged after {i} iterations")
                        break
                    else:
                        # Assign new values to latest slabs DataFrame
                        new_slabs["v_convergence_lat"], new_slabs["v_convergence_lon"] = functions_main.mag_azi2lat_lon(v_convergence_mag, new_slabs.trench_normal_azimuth); new_slabs["v_convergence_mag"] = v_convergence_mag
                        
                        # Delecte old DataFrames
                        del old_plates, old_points, old_slabs
                        
                        # Overwrite DataFrames
                        old_plates = new_plates.copy(); old_points = new_points.copy(); old_slabs = new_slabs.copy()

                # Calculate residual of plate velocities
                v_upper_plate_residual[i,j] = _numpy.max(abs(new_slabs.v_upper_plate_mag - true_slabs.v_upper_plate_mag))
                v_lower_plate_residual[i,j] = _numpy.max(abs(new_slabs.v_lower_plate_mag - true_slabs.v_lower_plate_mag))
                v_convergence_residual[i,j] = _numpy.max(abs(new_slabs.v_convergence_mag - true_slabs.v_convergence_mag))

        # Find the indices of the minimum value directly using _numpy.argmin
        opt_upper_plate_i, opt_upper_plate_j = _numpy.unravel_index(_numpy.argmin(v_upper_plate_residual), v_upper_plate_residual.shape)
        opt_upper_plate_visc = viscs[opt_upper_plate_i]
        opt_upper_plate_sp_const = sp_consts[opt_upper_plate_j]

        opt_lower_plate_i, opt_lower_plate_j = _numpy.unravel_index(_numpy.argmin(v_lower_plate_residual), v_lower_plate_residual.shape)
        opt_lower_plate_visc = viscs[opt_lower_plate_i]
        opt_lower_plate_sp_const = sp_consts[opt_lower_plate_j]

        opt_convergence_i, opt_convergence_j = _numpy.unravel_index(_numpy.argmin(v_convergence_residual), v_convergence_residual.shape)
        opt_convergence_visc = viscs[opt_convergence_i]
        opt_convergence_sp_const = sp_consts[opt_convergence_j]

        # Plot
        for i, j, visc, sp_const, residual in zip([opt_upper_plate_i, opt_lower_plate_i, opt_convergence_i], [opt_upper_plate_j, opt_lower_plate_j, opt_convergence_j], [opt_upper_plate_visc, opt_lower_plate_visc, opt_convergence_visc], [opt_upper_plate_sp_const, opt_lower_plate_sp_const, opt_convergence_sp_const], [v_upper_plate_residual, v_lower_plate_residual, v_convergence_residual]):
            if plot == True:
                fig, ax = plt.subplots(figsize=(15*self.constants.cm2in, 12*self.constants.cm2in))
                im = ax.imshow(residual, cmap="cmc.davos_r")#, vmin=-1.5, vmax=1.5)
                ax.set_yticks(_numpy.linspace(0, grid_size - 1, 5))
                ax.set_xticks(_numpy.linspace(0, grid_size - 1, 5))
                ax.set_xticklabels(["{:.2e}".format(visc) for visc in _numpy.linspace(visc_range[0], visc_range[1], 5)])
                ax.set_yticklabels(["{:.2f}".format(sp_const) for sp_const in _numpy.linspace(sp_consts.min(), sp_consts.max(), 5)])
                ax.set_xlabel("Mantle viscosity [Pa s]")
                ax.set_ylabel("Slab pull reduction factor")
                ax.scatter(j, i, marker="*", facecolor="none", edgecolor="k", s=30)
                fig.colorbar(im, label = "Residual velocity magnitude [cm/a]")
                plt.show()

            print(f"Optimal coefficients for ", ", ".join(new_plates.name.astype(str)), " plate(s), (PlateIDs: ", ", ".join(new_plates.plateID.astype(str)), ")")
            print("Minimum residual torque: {:.2%} of driving torque".format(10**(_numpy.amin(residual))))
            print("Optimum viscosity [Pa s]: {:.2e}".format(visc))
            print("Optimum Drag Coefficient [Pa s/m]: {:.2e}".format(visc / self.mech.La))
            print("Optimum Slab Pull constant: {:.2%}".format(sp_const))

    def optimise_torques(self, sediments=True):
        """
        Function to apply optimised parameters to torques
        Arguments:
            opt_visc
            opt_sp_const
        """
        # Apply to each torque in DataFrame
        axes = ["_x", "_y", "_z", "_mag"]
        for case in self.cases:
            for axis in axes:
                self.torques[case]["slab_pull_torque_opt" + axis] = self.options[case]["Slab pull constant"] * self.torques[case]["slab_pull_torque" + axis]
                if self.options[case]["Reconstructed motions"]:
                    self.torques[case]["mantle_drag_torque_opt" + axis] = self.options[case]["Mantle viscosity"] * self.torques[case]["mantle_drag_torque" + axis]
                
                for reconstruction_time in self.times:
                    if sediments == True:
                        self.plates[reconstruction_time][case]["slab_pull_torque_opt" + axis] = self.options[case]["Slab pull constant"] * self.plates[reconstruction_time][case]["slab_pull_torque" + axis]
                    if self.options[case]["Reconstructed motions"]:
                        self.plates[reconstruction_time][case]["mantle_drag_torque_opt" + axis] = self.options[case]["Mantle viscosity"] * self.plates[reconstruction_time][case]["mantle_drag_torque" + axis]

        # Apply to forces at centroid
        coords = ["lon", "lat"]
        for reconstruction_time in self.times:
            for case in self.cases:
                for coord in coords:
                    self.plates[reconstruction_time][case]["slab_pull_force_opt" + coord] = self.options[case]["Slab pull constant"] * self.plates[reconstruction_time][case]["slab_pull_force" + coord]
                    if self.options[case]["Reconstructed motions"]:
                        self.plates[reconstruction_time][case]["mantle_drag_force_opt" + coord] = self.options[case]["Mantle viscosity"] * self.plates[reconstruction_time][case]["slab_pull_force" + coord]

        self.optimised_torques = True

    def plot_normalised_residual_torque(self, ax, fig, opt_time, opt_case, plotting_options):
        """
        Plots the normalized residual torques on a given axis.

        :param ax:                  The axis on which to plot the torques.
        :type ax:                   matplotlib.axes.Axes
        :param fig:                 The figure to which the axis belongs.
        :type fig:                  matplotlib.figure.Figure
        :param opt_time:            The index of the optimal time.
        :type opt_time:             int
        :param opt_case:            The index of the optimal case.
        :type opt_case:             int
        :param plotting_options:    A dictionary containing various plotting options.
        :type plotting_options:     dict

        :return:                    The axis and the image.
        :rtype:                     matplotlib.axes.Axes, matplotlib.image.AxesImage
        """
        im = ax.imshow(self.residual_torque_normalised[opt_time][opt_case], cmap="cmc.lapaz_r", vmin=-1.5, vmax=1.5)
        ax.set_yticks(_numpy.linspace(0, grid_size - 1, 5))
        ax.set_xticks(_numpy.linspace(0, grid_size - 1, 5))
        ax.set_xticklabels(["{:.2e}".format(visc) for visc in _numpy.linspace(visc_range[0], visc_range[1], 5)])
        ax.set_yticklabels(["{:.2f}".format(sp_const) for sp_const in _numpy.linspace(sp_consts.min(), sp_consts.max(), 5)])
        ax.set_xlabel("Mantle viscosity [Pa s]")
        ax.set_ylabel("Slab pull reduction factor")
        ax.scatter(self.opt_j[opt_time][opt_case], self.opt_i[opt_time][opt_case], marker="*", facecolor="none", edgecolor="k", s=30)
        if plotting_options["cbar"] is True:
            fig.colorbar(im, label = "Log(residual torque/driving torque)")

        return ax, im

# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# SAVING 
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def save_all(self):
        for reconstruction_time in self.times:
            for case in self.cases:
                setup.DataFrame_to_csv(self.plates[reconstruction_time][case], "Plates", self.name, reconstruction_time, case, self.dir_path)
                setup.DataFrame_to_csv(self.slabs[reconstruction_time][case], "Slabs", self.name, reconstruction_time, case, self.dir_path)
                setup.DataFrame_to_csv(self.points[reconstruction_time][case], "Points", self.name, reconstruction_time, case, self.dir_path)
                setup.DataSet_to_netCDF(self.seafloor[reconstruction_time][case], "Seafloor_grid", self.name, reconstruction_time, case, self.dir_path)

        print(f"All data saved to {self.dir_path}!")

# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# PLOTTING 
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    
    def plot_age_map(self, ax, fig, reconstruction_time: int, plotting_options: dict):
        """
        Function to create subplot with global seafloor age
            plotting_options:   dictionary with options for plotting
        """
        # Check if reconstruction time is in valid times
        if reconstruction_time not in self.times:
            return print("Invalid reconstruction time")
        
        # Set basemap
        ax, gl = self.plot_basemap(ax)

        # Plot age
        ages = ax.imshow(
            self.seafloor.sel(age=reconstruction_time).seafloor_age.values,
            cmap = plotting_options["age cmap"],
            transform=ccrs.PlateCarree(), 
            zorder=1, 
            vmin=0, 
            vmax=plotting_options["age max"], 
            origin="lower"
        )

        # Plot plates and coastlines
        self.plot_reconstruction(ax, reconstruction_time, plotting_options, plates=True, trenches=True)

        # Colourbar
        if plotting_options["cbar"] is True:
            fig.colorbar(ages, ax=ax, label="Seafloor age [Ma]", orientation=plotting_options["orientation cbar"], shrink=0.75, aspect=20)

        return ax, ages

    def plot_sediment_map(self, ax, fig, reconstruction_time: int, case, plotting_options: dict):
        """
        Function to create subplot with global sediment thicknesses
            case:               case for which to plot the sediments
            plotting_options:   dictionary with options for plotting
        """
        # Check if reconstruction time is in valid times
        if reconstruction_time not in self.times:
            return print("Invalid reconstruction time")
        
        # Set basemap
        ax, gl = self.plot_basemap(ax)

        if self.options[case]["Sample sediment grid"] !=0:
            raster = self.seafloor.sel(age=reconstruction_time)[self.options[case]["Sample sediment grid"]]
        else:
            raster = _numpy.where(_numpy.isnan(self.seafloor.sel(age=reconstruction_time).seafloor_age), _numpy.nan, 0)

        # Plot sediment
        seds = ax.imshow(
            raster,
            cmap = plotting_options["sediment cmap"],
            transform=ccrs.PlateCarree(), 
            zorder=1, 
            vmin=0, 
            vmax=plotting_options["sediment max"], 
            origin="lower"
        )

        if self.options[case]["Active margin sediments"] != 0:
            data = self.slabs[reconstruction_time][case]
            slab_data = ax.scatter(
                data.lon,
                data.lat,
                c=data.sediment_thickness,
                s=plotting_options["marker size"],
                transform=ccrs.PlateCarree(),
                cmap=plotting_options["sediment cmap"],
                vmin=0,
                vmax=plotting_options["sediment max"]
            )

        # Plot plates and coastlines
        self.plot_reconstruction(ax, reconstruction_time, plotting_options, plates=True, trenches=True)

        # Colourbar
        if plotting_options["cbar"] is True:
            fig.colorbar(seds, ax=ax, label="Sediment thickness [m]", orientation=plotting_options["orientation cbar"], shrink=0.75, aspect=20)
            
        return ax, seds
    
    def plot_velocity_map(self, ax, fig, reconstruction_time, case, plotting_options):
        """
        Function to create subplot with plate velocities
            ax:                     axes object
            fig:                    figure
            reconstruction_time:    the time for which to display the map
            case:                   case for which to plot the sediments
            plotting_options:       dictionary with options for plotting
        """
        # Check if reconstruction time is in valid times
        if reconstruction_time not in self.times:
            return print("Invalid reconstruction time")
        
        # Set basemap
        ax, gl = self.plot_basemap(ax)

        # Plot plates and coastlines
        self.plot_reconstruction(ax, reconstruction_time, plotting_options, plates=True, trenches=False)

        # Get data
        plate_vectors = self.plates[reconstruction_time][case].loc[self.plates[reconstruction_time][case].area >= self.options[case]["Minimum plate area"]]
        slab_data = self.slabs[reconstruction_time][case].loc[self.slabs[reconstruction_time][case].lower_plateID.isin(plate_vectors.plateID)]
        slab_vectors = slab_data.iloc[::5]

        # Plot velocity magnitude at trenches
        if self.options[case]["Reconstructed motions"]:
            variable = "v_absolute_mag"
        else:
            variable = "v_synthetic_mag"
        vels = ax.scatter(
            slab_data.lon,
            slab_data.lat,
            c=slab_data[variable],
            s=plotting_options["marker size"],
            transform=ccrs.PlateCarree(),
            cmap=plotting_options["velocity magnitude cmap"],
            vmin=0,
            vmax=plotting_options["velocity max"]
        )

        # Plot velocity at subduction zones
        if self.options[case]["Reconstructed motions"]:
            variable = "v_absolute"
        else:
            variable = "v_synthetic"
        slab_vectors = ax.quiver(
            x=slab_vectors.lon,
            y=slab_vectors.lat,
            u=slab_vectors[variable + "_lon"],
            v=slab_vectors[variable + "_lat"],
            transform=ccrs.PlateCarree(),
            # label=vector.capitalize(),
            width=2e-3,
            scale=3e2,
            zorder=4,
            color='black'
        )

        # Plot velocity at centroid
        if self.options[case]["Reconstructed motions"]:
            variable = "v_absolute"
        else:
            variable = "v_synthetic"
        centroid_vectors = ax.quiver(
            x=plate_vectors.centroid_lon,
            y=plate_vectors.centroid_lat,
            u=plate_vectors[variable + "_lon"],
            v=plate_vectors[variable + "_lat"],
            transform=ccrs.PlateCarree(),
            # label=vector.capitalize(),
            width=5e-3,
            scale=3e2,
            zorder=4,
            color='white',
            edgecolor='black',
            linewidth=1
        )

        # Colourbar
        if plotting_options["cbar"] is True:
            fig.colorbar(vels, ax=ax, label="Velocity [cm/a]", orientation=plotting_options["orientation cbar"], shrink=0.75, aspect=20)
    
        return ax, vels, centroid_vectors, slab_vectors

    def plot_velocity_difference_map(self, ax, fig, reconstruction_time, case1, case2, plotting_options):
        """
        Function to create subplot with difference between plate velocity at trenches between two cases
            case:               case for which to plot the sediments
            plotting_options:   dictionary with options for plotting
        """

        # Check if reconstruction time is in valid times
        if reconstruction_time not in self.times:
            return print("Invalid reconstruction time")
        
        # Set basemap
        ax, gl = self.plot_basemap(ax)

        # Plot plates and coastlines
        self.plot_reconstruction(ax, reconstruction_time, plotting_options, plates=True, trenches=False)

        # Get data
        plate_vectors = {}
        slab_data = {}
        slab_vectors = {}
        for case in [case1, case2]:
            plate_vectors[case] = self.plates[reconstruction_time][case].loc[self.plates[reconstruction_time][case].area >= self.options[case]["Minimum plate area"]]
            slab_data[case] = self.slabs[reconstruction_time][case].loc[self.slabs[reconstruction_time][case].lower_plateID.isin(plate_vectors[case].plateID)]
            slab_vectors[case] = slab_data[case].iloc[::5]
        
        # Plot velocity magnitude at trenches
        if self.options[case1]["Reconstructed motions"]:
            var1 = "v_absolute_"
        else:
            var1 = "v_synthetic_"
        if self.options[case2]["Reconstructed motions"]:
            var2 = "v_absolute_"
        else:
            var2 = "v_synthetic_"
        vels = ax.scatter(
            slab_data[case1].lon,
            slab_data[case1].lat,
            c=slab_data[case1][var1 + "mag"] - slab_data[case2][var2 + "mag"],
            s=plotting_options["marker size"],
            transform=ccrs.PlateCarree(),
            cmap=plotting_options["velocity difference cmap"],
            vmin=-plotting_options["velocity max"]/2,
            vmax=plotting_options["velocity max"]/2
        )

        # Plot velocity at subduction zones
        slab_vectors = ax.quiver(
            x=slab_vectors[case1].lon,
            y=slab_vectors[case1].lat,
            u=slab_vectors[case1][var1 + "lon"] - slab_vectors[case2][var2 + "lon"],
            v=slab_vectors[case1][var1 + "lat"] - slab_vectors[case2][var2 + "lat"],
            transform=ccrs.PlateCarree(),
            # label=vector.capitalize(),
            width=2e-3,
            scale=3e2,
            zorder=4,
            color='black'
        )

        # Plot velocity at centroid
        if self.options[case1]["Reconstructed motions"]:
            variable1 = "v_absolute"
        else:
            variable1 = "v_synthetic"
        if self.options[case2]["Reconstructed motions"]:
            variable2 = "v_absolute"
        else:
            variable2 = "v_synthetic"
        centroid_vectors = ax.quiver(
            x=plate_vectors[case1].centroid_lon,
            y=plate_vectors[case1].centroid_lat,
            u=plate_vectors[case1][variable1 + "_lon"] - plate_vectors[case2][variable2 + "_lon"],
            v=plate_vectors[case1][variable1 + "_lat"] - plate_vectors[case2][variable2 + "_lat"],
            transform=ccrs.PlateCarree(),
            # label=vector.capitalize(),
            width=5e-3,
            scale=3e2,
            zorder=4,
            color='white',
            edgecolor='black',
            linewidth=1
        )

        # Colourbar
        if plotting_options["cbar"] is True:
            fig.colorbar(vels, ax=ax, label="Velocity difference [cm/a]", orientation=plotting_options["orientation cbar"], shrink=0.75, aspect=20)
    
        return ax, vels, slab_vectors, centroid_vectors
    
    def plot_relative_velocity_difference_map(self, ax, fig, reconstruction_time, case1, case2, plotting_options):
        """
        Function to create subplot with difference between plate velocity at trenches between two cases
            case:               case for which to plot the sediments
            plotting_options:   dictionary with options for plotting
        """

        # Check if reconstruction time is in valid times
        if reconstruction_time not in self.times:
            return print("Invalid reconstruction time")
        
        # Set basemap
        ax, gl = self.plot_basemap(ax)

        # Plot plates and coastlines
        self.plot_reconstruction(ax, reconstruction_time, plotting_options, plates=True, trenches=False)

        # Get data
        plate_vectors = {}
        slab_data = {}
        slab_vectors = {}
        for case in [case1, case2]:
            plate_vectors[case] = self.plates[reconstruction_time][case].loc[self.plates[reconstruction_time][case].area >= self.options[case]["Minimum plate area"]]
            slab_data[case] = self.slabs[reconstruction_time][case].loc[self.slabs[reconstruction_time][case].lower_plateID.isin(plate_vectors[case].plateID)]
            slab_vectors[case] = slab_data[case].iloc[::5]
        
        # Plot velocity magnitude at trenches
        if self.options[case1]["Reconstructed motions"]:
            var1 = "v_absolute_"
        else:
            var1 = "v_synthetic_"
        if self.options[case2]["Reconstructed motions"]:
            var2 = "v_absolute_"
        else:
            var2 = "v_synthetic_"
        vels = ax.scatter(
            slab_data[case1].lon,
            slab_data[case1].lat,
            c=slab_data[case1][var1 + "mag"] / slab_data[case2][var2 + "mag"],
            s=plotting_options["marker size"],
            transform=ccrs.PlateCarree(),
            cmap=plotting_options["relative velocity difference cmap"],
            vmin=1,
            vmax=plotting_options["relative velocity max"]
        )

        # relative_slab_vectors_lon = (slab_vectors[case1][var1 + "lon"] / slab_vectors[case2][var2 + "mag"])
        # relative_slab_vectors_lon = _numpy.where(relative_slab_vectors_lon > 0, relative_slab_vectors_lon-1, relative_slab_vectors_lon+1)
        # relative_slab_vectors_lat = (slab_vectors[case1][var1 + "lat"] / slab_vectors[case2][var2 + "mag"])
        # relative_slab_vectors_lat = _numpy.where(relative_slab_vectors_lat > 0, relative_slab_vectors_lat-1, relative_slab_vectors_lat+1)

        # Plot velocity at subduction zones
        slab_vectors = ax.quiver(
            x=slab_vectors[case1].lon,
            y=slab_vectors[case1].lat,
            u=(slab_vectors[case1][var1 + "lon"] - slab_vectors[case2][var2 + "lon"]) / slab_vectors[case2][var2 + "mag"] * 10,
            v=(slab_vectors[case1][var1 + "lat"] - slab_vectors[case2][var2 + "lat"]) / slab_vectors[case2][var2 + "mag"] * 10,
            transform=ccrs.PlateCarree(),
            # label=vector.capitalize(),
            width=2e-3,
            scale=3e2,
            zorder=4,
            color='black'
        )

        # relative_plate_vectors_lon = (plate_vectors[case1][var1 + "lon"] / plate_vectors[case2][var2 + "mag"])
        # relative_plate_vectors_lon = _numpy.where(relative_plate_vectors_lon > 0, relative_plate_vectors_lon-1, relative_plate_vectors_lon+1)
        # relative_plate_vectors_lat = (plate_vectors[case1][var1 + "lat"] / plate_vectors[case2][var2 + "mag"])
        # relative_plate_vectors_lat = _numpy.where(relative_plate_vectors_lat > 0, relative_plate_vectors_lat-1, relative_plate_vectors_lat+1)

        # Plot velocity at centroid
        if self.options[case1]["Reconstructed motions"]:
            variable1 = "v_absolute"
        else:
            variable1 = "v_synthetic"
        if self.options[case2]["Reconstructed motions"]:
            variable2 = "v_absolute"
        else:
            variable2 = "v_synthetic"
        centroid_vectors = ax.quiver(
            x=plate_vectors[case1].centroid_lon,
            y=plate_vectors[case1].centroid_lat,
            u=(plate_vectors[case1][var1 + "lon"] - plate_vectors[case2][var2 + "lon"]) / plate_vectors[case2][var2 + "mag"] * 10,
            v=(plate_vectors[case1][var1 + "lat"] - plate_vectors[case2][var2 + "lat"]) / plate_vectors[case2][var2 + "mag"] * 10,
            transform=ccrs.PlateCarree(),
            # label=vector.capitalize(),
            width=5e-3,
            scale=3e2,
            zorder=4,
            color='white',
            edgecolor='black',
            linewidth=1
        )

        # Colourbar
        if plotting_options["cbar"] is True:
            fig.colorbar(vels, ax=ax, label="Relative velocity difference", orientation=plotting_options["orientation cbar"], shrink=0.75, aspect=20)
    
        return ax, vels, slab_vectors, centroid_vectors

    def plot_basemap(self, ax):
        # Set labels
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")

        # Set global extent
        ax.set_global()

        # Set gridlines
        gl = ax.gridlines(
            crs=ccrs.PlateCarree(), 
            draw_labels=True, 
            linewidth=0.5, 
            color="gray", 
            alpha=0.5, 
            linestyle="--", 
            zorder=5
        )

        gl.top_labels = False
        gl.right_labels = False  

        return ax, gl
    
    def plot_reconstruction(self, ax, reconstruction_time: int, plotting_options: dict, coastlines=True, plates=False, trenches=False):
        """
        Function to plot reconstructed features: coastlines, plates and trenches
        """
        # Set gplot object
        gplot = gplately.PlotTopologies(self.reconstruction, time=reconstruction_time, coastlines=self.coastlines)

        # Plot coastlines
        if coastlines:
            gplot.plot_coastlines(ax, color="lightgrey", zorder=-5)
        
        # Plot plates (NOTE: polygons do NOT cross the dateline)
        if plates:
            gplot.plot_all_topologies(ax, lw=plotting_options["linewidth plate boundaries"])
            
        # Plot trenches
        if plates and trenches:
            gplot.plot_subduction_teeth(ax)

        return ax
    
    def plot_plate_motions(self, ax, reconstruction_time: int, plotting_options: dict, coastlines=True, plates=True, trenches=True):

        # Plot the reconstruction
        self.plot_reconstruction(ax, reconstruction_time, plotting_options, plates=True, trenches=True)

        # Add plate motion vectors

    
    def plot_torque_through_time(self, ax, selected_case=None, selected_times=None):
        
        # Get times
        if selected_times == None:
            selected_times = self.reconstruction_times

        # Get cases
        if selected_cases == None:
            selected_cases = self.cases

        # Initialise dictionaries to store data

        # for reconstruction_time in selected_times:
        


        # Generate listed colormap

        # Plot
        # for case in cases:
        #     ax.plot()

        # return ax 

# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# RANDOMISATION
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    # def randomise_trench_azimuth(plateID):
    #     random_value = _numpy.random.normal(0, 2.5)
    #     return plateID, random_value

    # def randomise_slab_age(plateID):
        
    #     return plateID, random_value

# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# PARALLELISATION
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def run_parallel(self, function_to_run):
        num_processes = multiprocessing.cpu_count()
        pool = multiprocessing.Pool(processes=num_processes)

        for reconstruction_time in self.times:
            print(f"Running {function_to_run.__name__} at {reconstruction_time} Ma")
            for case in self.cases:
                pool.apply_async(function_to_run, args=(reconstruction_time, case))

        pool.close()
        pool.join()