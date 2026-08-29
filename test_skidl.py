from skidl import *
import os

# Create input & output voltages and ground reference
vin, vout, gnd = Net('VI'), Net('VO'), Net('GND')

# Create two resistors with values and footprints
r1, r2 = 2 * Part("Device", 'R', dest=TEMPLATE, footprint='Resistor_SMD:R_0805_2012Metric')
r1.value, r2.value = '1K', '500'

u1 = Part("U",
    "Value": "TLV702475_SOT23-5",
    "Footprint": "Package_TO_SOT_SMD:SOT-23-5",)

# Connect the circuit elements.
vin & r1 & vout & r2 & gnd

# Or connect pin-by-pin if you prefer
# vin += r1[1]
# vout += r1[2], r2[1] 
# gnd += r2[2]

# Check for errors and generate netlist
ERC()
generate_netlist(tool=KICAD8)