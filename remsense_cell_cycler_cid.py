import pyvisa
import pyvisa.constants as visa_const
import usbtmc
import minimalmodbus
import pyudev
import time
import csv
from datetime import datetime
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
import subprocess
import warnings

# Suppress the very common (and harmless) pyvisa warning from BK 8514B
warnings.filterwarnings(
    "ignore",
    message=r"read string doesn't end with termination characters",
    category=UserWarning,
    module=r"pyvisa"
)

# ==================== CONFIGURATION ====================
NUMBER_OF_CYCLES        = 2

# --- Charging Settings ---
CHARGE_VOLTAGE_LIMIT    = 4.2
TARGET_CHARGE_VOLTAGE   = 3.75
CHARGE_CURRENT          = 1.0
MAX_CHARGE_TIME         = 600
CHARGE_REST_TIME        = 150

# --- Discharging Settings ---
DISCHARGE_CURRENT       = 2.0
DISCHARGE_CUTOFF_V      = 2.75
DISCHARGE_REST_TIME     = 150

# --- Safety Features ---
MAX_TEMPERATURE         = 45.0
MIN_CAPACITY_RETENTION  = 0.0

# --- Fan Control ---
FAN_GPIO                = 17
FAN_CHIP                = "gpiochip0"
TEMP_FAN_THRESHOLD      = 24.0   # °C

LOG_FILE = "battery_test_log.csv"
FINAL_PLOT_FILE = "battery_cycle_final_plot.png"
# ==================================================

# === Live Plot Setup ===
plot_times = []
plot_voltages = []
plot_temps = []
plot_phases = []
fig = None
ax1 = None
ax2 = None
global_start_time = None

def init_live_plot():
    global fig, ax1, ax2, global_start_time
    global_start_time = time.time()
    plt.ion()
    fig, ax1 = plt.subplots(figsize=(10, 6))
    ax2 = ax1.twinx()
    ax2.yaxis.set_label_position("right")
    ax1.set_xlabel('Elapsed Time (seconds)')
    ax1.set_ylabel('Voltage (V)', color='tab:blue')
    ax2.set_ylabel('Temperature (°C)', color='tab:red')
    ax1.set_title('Battery Cell Cycler - Live Plot')
    ax1.grid(True, alpha=0.3)
    plt.tight_layout()
    return fig, ax1, ax2

def update_live_plot(phase):
    global fig, ax1, ax2
    if fig is None:
        return
    ax1.cla()
    ax2.cla()
    ax1.set_xlabel('Elapsed Time (seconds)')
    ax1.set_ylabel('Voltage (V)', color='tab:blue')
    ax2.yaxis.set_label_position("right")
    ax2.set_ylabel('Temperature (°C)', color='tab:red')
    ax1.set_title(f'Battery Cell Cycler - Live Plot (Cycle {current_cycle} - {phase})')
    ax1.grid(True, alpha=0.3)
    if len(plot_times) > 0:
        i = 0
        while i < len(plot_times):
            j = i
            while j < len(plot_times) and plot_phases[j] == plot_phases[i]:
                j += 1
            color = 'tab:blue' if plot_phases[i] == 'CHARGE' else 'tab:orange'
            ax1.plot(plot_times[i:j], plot_voltages[i:j], color=color, linewidth=2)
            i = j
        ax2.plot(plot_times, plot_temps, color='tab:red', linestyle='--', linewidth=1.5)
    ax1.relim()
    ax1.autoscale_view(True)
    ax2.relim()
    ax2.autoscale_view(True)
    ax1.margins(y=0.08)
    ax2.margins(y=0.08)
    fig.canvas.draw()
    fig.canvas.flush_events()
    plt.pause(0.01)

def save_final_plot():
    global fig
    if fig is not None:
        try:
            fig.savefig(FINAL_PLOT_FILE, dpi=300, bbox_inches='tight')
            print(f"\n✅ Final plot saved as '{FINAL_PLOT_FILE}'")
        except Exception as e:
            print(f"⚠️ Could not save final plot: {e}")

# === Fan Control Functions ===
def set_fan(state):
    """state: 1 = ON, 0 = OFF (active-HIGH on your relay)"""
    try:
        subprocess.run(
            ["timeout", "0.1s", "gpioset", f"--chip={FAN_CHIP}", f"{FAN_GPIO}={state}"],
            check=False,
            capture_output=True
        )
    except:
        pass

def fan_on():
    set_fan(1)

def fan_off():
    set_fan(0)

# === Helper Functions ===
def find_prolific_port():
    context = pyudev.Context()
    for device in context.list_devices(subsystem='tty'):
        if 'Prolific' in device.get('ID_VENDOR', ''):
            return device.device_node
    return None

def find_temperature_port(exclude_port=None):
    context = pyudev.Context()
    for device in context.list_devices(subsystem='tty'):
        vendor = device.get('ID_VENDOR', '')
        devnode = device.device_node
        if exclude_port and devnode == exclude_port:
            continue
        if any(x in vendor for x in ['FTDI', 'Silicon Labs', 'Prolific']) == False:
            if 'ID_VENDOR' in device:
                return devnode
    return None

def get_temperature(instrument):
    if instrument is None:
        return None
    try:
        temp_tenths = instrument.read_register(registeraddress=0x0F, number_of_decimals=0, functioncode=3, signed=True)
        return temp_tenths / 10.0
    except:
        return None

def countdown_sleep(seconds, message):
    for remaining in range(seconds, 0, -1):
        mins, secs = divmod(remaining, 60)
        print(f"\r{message} → {mins:02d}:{secs:02d} remaining", end="")
        time.sleep(1)
    print()

print("=== Remote Sense Cell Cycler with Live Plot + Fan Control (Clean Output) ===\n")

# === Instrument Setup ===
load_port = find_prolific_port()
load_resource = f'ASRL{load_port}::INSTR' if load_port else 'ASRL/dev/ttyUSB0::INSTR'

rm = pyvisa.ResourceManager('@py')
load = rm.open_resource(load_resource)
load.timeout = 8000
load.baud_rate = 9600
load.data_bits = 8
load.parity = visa_const.Parity.none
load.stop_bits = visa_const.StopBits.one
load.write_termination = '\n'
load.read_termination = '\r\n'
load.chunk_size = 1024          # Cleaner BK 8514B communication
time.sleep(0.8)

# Enable Remote Sense on 8514B
load.write("SYST:REM")
load.write("SOUR:SENS:REM ON")
time.sleep(0.5)

# 9206B PSU
psu = usbtmc.Instrument(0x2EC7, 0x9200)
psu.write("SYST:REM")
psu.write("SOUR:SENS:REM OFF")
time.sleep(0.5)

# Temperature Sensor
temp_instrument = None
temp_port = find_temperature_port(exclude_port=load_port)
if temp_port:
    try:
        temp_instrument = minimalmodbus.Instrument(temp_port, 1)
        temp_instrument.serial.baudrate = 9600
        temp_instrument.serial.bytesize = 8
        temp_instrument.serial.parity = minimalmodbus.serial.PARITY_NONE
        temp_instrument.serial.stopbits = 1
        temp_instrument.serial.timeout = 1.0
        temp_instrument.close_port_after_each_call = True
        print(f"✅ Auto-detected temperature sensor on {temp_port}")
    except Exception as e:
        print(f"⚠️  Temperature sensor issue: {e}")

print("8514B ID:", load.query('*IDN?').strip())
print("9206B ID:", psu.ask('*IDN?').strip())
print("\n✅ All instruments ready!\n")

current_cycle = 1

def run_battery_cycle(max_cycles=1):
    global current_cycle, plot_times, plot_voltages, plot_temps, plot_phases, fig, ax1, ax2
    first_discharge_capacity = None
    completed_cycles = 0

    fig, ax1, ax2 = init_live_plot()
    plot_times.clear()
    plot_voltages.clear()
    plot_temps.clear()
    plot_phases.clear()

    fan_off()
    print("✅ Fan relay initialized (OFF)")

    with open(LOG_FILE, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Timestamp', 'Cycle', 'Phase', 'Voltage_V', 'Current_A', 
                         'Power_W', 'Capacity_Ah', 'Temperature_C'])

        for cycle in range(1, max_cycles + 1):
            current_cycle = cycle
            print(f"\n{'='*55}")
            print(f"=== CYCLE {cycle}/{max_cycles} ===")
            print(f"{'='*55}")

            # === CHARGE PHASE ===
            print("\n>>> CHARGE PHASE STARTED")
            psu.write(f"VOLT {CHARGE_VOLTAGE_LIMIT}")
            psu.write(f"CURR {CHARGE_CURRENT}")
            psu.write("OUTP ON")

            start_time = time.time()
            while True:
                v = float(load.query('MEAS:VOLT?').strip())
                i = float(psu.ask("MEAS:CURR?").strip())
                temp = get_temperature(temp_instrument)

                # Temperature-based fan control
                if temp is not None:
                    if temp > TEMP_FAN_THRESHOLD:
                        fan_on()
                    else:
                        fan_off()

                if temp is not None and temp > MAX_TEMPERATURE:
                    print(f"\n🚨 SAFETY STOP: Temperature too high ({temp:.1f}°C)")
                    psu.write("OUTP OFF")
                    return

                elapsed = time.time() - global_start_time
                temp_str = f"{temp:.1f}" if temp is not None else ""
                
                writer.writerow([datetime.now().isoformat(), cycle, 'CHARGE', v, i, v*i,
                                 (time.time()-start_time)*i/3600, temp_str])
                
                plot_times.append(elapsed)
                plot_voltages.append(v)
                plot_temps.append(temp if temp is not None else 25.0)
                plot_phases.append('CHARGE')
                
                update_live_plot('CHARGE')

                print(f"     V={v:.3f}V  I={i:.3f}A  T={temp_str}°C", end='\r')

                if v >= TARGET_CHARGE_VOLTAGE:
                    print(f"\n>>> Target voltage reached ({v:.3f}V). Ending charge.")
                    break
                if time.time() - start_time > MAX_CHARGE_TIME:
                    print("\n   Max charge time reached.")
                    break
                time.sleep(3)

            psu.write("OUTP OFF")
            print(">>> CHARGE PHASE COMPLETE")

            # === CHARGE REST (force fan ON) ===
            print(f"\n>>> CHARGE REST ({CHARGE_REST_TIME}s)")
            fan_on()
            print("   Fan turned ON for cooling")
            countdown_sleep(CHARGE_REST_TIME, "CHARGE REST")
            fan_off()

            # === DISCHARGE PHASE ===
            print("\n>>> DISCHARGE PHASE STARTED")
            load.write(f'SOUR:CURR {DISCHARGE_CURRENT}')
            load.write('SOUR:CURR:MODE CC')
            load.write('INP ON')

            start_time = time.time()
            discharge_capacity = 0.0

            while True:
                v = float(load.query('MEAS:VOLT?').strip())
                i = float(load.query('MEAS:CURR?').strip())
                temp = get_temperature(temp_instrument)

                # Temperature-based fan control
                if temp is not None:
                    if temp > TEMP_FAN_THRESHOLD:
                        fan_on()
                    else:
                        fan_off()

                if temp is not None and temp > MAX_TEMPERATURE:
                    print(f"\n🚨 SAFETY STOP: Temperature too high ({temp:.1f}°C)")
                    load.write('INP OFF')
                    return

                elapsed = time.time() - global_start_time
                temp_str = f"{temp:.1f}" if temp is not None else ""
                
                writer.writerow([datetime.now().isoformat(), cycle, 'DISCHARGE', v, i, v*i,
                                 (time.time()-start_time)*i/3600, temp_str])
                
                plot_times.append(elapsed)
                plot_voltages.append(v)
                plot_temps.append(temp if temp is not None else 25.0)
                plot_phases.append('DISCHARGE')
                
                update_live_plot('DISCHARGE')

                print(f"     V={v:.3f}V  I={i:.3f}A  T={temp_str}°C", end='\r')

                discharge_capacity = (time.time() - start_time) * i / 3600
                if v <= DISCHARGE_CUTOFF_V:
                    break
                time.sleep(2)

            load.write('INP OFF')
            print(">>> DISCHARGE PHASE COMPLETE")

            if cycle == 1:
                first_discharge_capacity = discharge_capacity
            elif first_discharge_capacity and MIN_CAPACITY_RETENTION > 0:
                retention = discharge_capacity / first_discharge_capacity
                if retention < MIN_CAPACITY_RETENTION:
                    print(f"\n🚨 STOPPING TEST: Capacity retention too low ({retention*100:.1f}%)")
                    break

            # === DISCHARGE REST (force fan ON) ===
            print(f"\n>>> DISCHARGE REST ({DISCHARGE_REST_TIME}s)")
            fan_on()
            print("   Fan turned ON for cooling")
            countdown_sleep(DISCHARGE_REST_TIME, "DISCHARGE REST")
            fan_off()

            completed_cycles = cycle

    print(f"\n✅ Test finished after {completed_cycles} cycle(s).")
    save_final_plot()

if __name__ == "__main__":
    try:
        run_battery_cycle(max_cycles=NUMBER_OF_CYCLES)
    finally:
        try:
            psu.write("OUTP OFF")
            load.write('INP OFF')
            fan_off()
        except:
            pass
        load.close()
        psu.close()
        if temp_instrument:
            try:
                temp_instrument.serial.close()
            except:
                pass
        save_final_plot()
        plt.close('all')
        print("Instruments and fan safely closed.")