import subprocess
import serial
import pynmea2
import time
import os
import json
from datetime import datetime

PORT_GPS = '/dev/ttyUSB1'
BAUDRATE = 9600
LOG_DIR = '/home/skytrac/AFF/PiTrac/data'
LOG_INTERVAL = 10
PROVIDER = "SKYTRAC"

EVENT_CODES = {
    'Position Report': 0,
    'POWER ON': 1,
    'POWER OFF': 2,  # reserved
    'MOVING': 3,
    'STOPPED': 4,
}

def get_imei_mmcli():
    try:
        result = subprocess.run(['mmcli', '-m', '0'], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if 'equipment id' in line:
                return line.split(':')[-1].strip()
    except Exception as e:
        print(f"Failed to get IMEI: {e}")
    return "UNKNOWN"

def parse_gps(ser):
    gps_data = {
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'posTime': None,
        'lat': None,
        'lon': None,
        'alt': None,
        'course': 0,
        'speed_m_s': 0.0,
        'pdop': None,
        'fix_type': "Invalid",
        'valid': False,
        'sats': None
    }

    start_time = time.time()
    while time.time() - start_time < 5:
        try:
            line = ser.readline().decode('ascii', errors='ignore').strip()
            if not line.startswith('$'):
                continue

            if 'GSA' in line:
                parts = line.split(',')
                if len(parts) >= 16:
                    gps_data['pdop'] = float(parts[15]) if parts[15] else None
                    fix = parts[2]
                    if fix == '2':
                        gps_data['fix_type'] = "2D"
                    elif fix == '3':
                        gps_data['fix_type'] = "3D"

            msg = pynmea2.parse(line)

            if isinstance(msg, pynmea2.types.talker.RMC):
                if msg.status == 'A':
                    gps_data['valid'] = True
                if msg.latitude and msg.longitude:
                    gps_data['lat'] = float(msg.latitude)
                    gps_data['lon'] = float(msg.longitude)
                gps_data['course'] = round(float(msg.true_course), 1) if msg.true_course else 0
                knots = float(msg.spd_over_grnd) if msg.spd_over_grnd else 0.0
                gps_data['speed_m_s'] = round(knots * 0.514444, 1)  # convert to m/s
                gps_data['posTime'] = msg.datestamp.strftime('%Y-%m-%d') + 'T' + msg.timestamp.strftime('%H:%M:%S') + 'Z'

            elif isinstance(msg, pynmea2.types.talker.GGA):
                gps_data['alt'] = float(msg.altitude) if msg.altitude else 0.0
                gps_data['sats'] = int(msg.num_sats) if msg.num_sats else None

        except Exception:
            continue

    return gps_data

def create_aff_feature(gps, imei, event_code, event_type, extra_data):
    return {
        "type": "Feature",
        "properties": {
            "rpt": "pos",
            "esn": imei,
            "unitId": "SKYTRAC_UNIT",
            "cog": int(gps['course']),
            "spd": int(gps['speed_m_s']),
            "src": "GPS",
            "fix": gps['fix_type'],
            "pdop": gps['pdop'],
            "posTime": gps['posTime'],
            "dataCtrTime": gps['timestamp'],
            "ctrId": "skytrac-pi",
            "event_code": event_code,
            "event_type": event_type,
            "extra_data": extra_data
        },
        "geometry": {
            "type": "Point",
            "coordinates": [gps['lon'], gps['lat'], gps['alt']]
        }
    }

if __name__ == '__main__':
    imei = get_imei_mmcli()
    print(f"IMEI: {imei}")

    # log file name
    log_start = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    json_path = os.path.join(LOG_DIR, f"{log_start}.json")
    os.makedirs(LOG_DIR, exist_ok=True)

    geojson_data = {
        "type": "FeatureCollection",
        "dataInfo": [{
            "affVer": "json 1.0",
            "provider": PROVIDER,
            "rptTime": datetime.utcnow().isoformat() + 'Z',
            "sysId": "skytrac-pi"
        }],
        "features": []
    }

    prev_speed = 0.0
    power_on_logged = False
    in_motion = False

    with serial.Serial(PORT_GPS, BAUDRATE, timeout=1) as gps_ser:
        while True:
            gps = parse_gps(gps_ser)

            if gps['valid'] and gps['lat'] and gps['lon']:
                event_type = 'Position Report'

                if not power_on_logged:
                    event_type = 'POWER ON'
                    power_on_logged = True
                    in_motion = gps['speed_m_s'] > 0
                else:
                    if not in_motion and gps['speed_m_s'] > 0:
                        event_type = 'MOVING'
                        in_motion = True
                    elif in_motion and gps['speed_m_s'] == 0:
                        event_type = 'STOPPED'
                        in_motion = False

                event_code = EVENT_CODES[event_type]
                feature = create_aff_feature(gps, imei, event_code, event_type, "")
                geojson_data["features"].append(feature)

                with open(json_path, 'w') as f:
                    json.dump(geojson_data, f, indent=2)

                print(f"[{gps['timestamp']}] {event_type} - {gps['lat']}, {gps['lon']} ({gps['speed_m_s']} m/s)")
                prev_speed = gps['speed_m_s']
            else:
                print(f"[{datetime.utcnow().isoformat()}] No fix")

            time.sleep(LOG_INTERVAL)
