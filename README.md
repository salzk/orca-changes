# orca-changes
Simple CLI utility to list OrcaSlicer values that were changed from system values.

OrcaSlicers stores all values that are different from the values in system profiles 
in the local profile (and only these). This scripts lists these values and also the
original value in the system profile. It fetches the original profiles from the
OrcaSlicer GitHub repository and caches them locally for efficient access.
