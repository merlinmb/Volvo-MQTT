# Volvo Node-RED Flow: Grafana and InfluxDB

This guide describes the flow in [`nodered-volvo.json`](nodered-volvo.json). The flow expects the Volvo bridge's default MQTT prefix, `volvo/vehicle`, and the MQTT/InfluxDB configuration nodes referenced by that export.

## Data flow

| MQTT input | Payload from the Volvo bridge | Flow behavior |
|---|---|---|
| `volvo/vehicle/sensor/#` | One scalar per topic. Numeric readings and booleans are published as text, for example `87`, `true`, or `false`. | The router accepts the numeric metrics and `any_open`; it handles `lock_status` separately. Other sensor topics are ignored. |
| `volvo/vehicle/location` | JSON from the Location API. Coordinates are in `payload.data.geometry.coordinates` as `[longitude, latitude, altitude]`; heading is in `payload.data.properties.heading`. | Extracts longitude, latitude, and heading and sends numeric fields to the location InfluxDB output. Altitude is not saved. |

The bridge publishes sensor values as retained MQTT messages, so Node-RED receives the latest value after connecting. Location is only available when location polling is enabled and a location update has been published.

## Environmentals measurements

The telemetry InfluxDB output uses the `environmentals/autogen` bucket. The router sets `msg.measurement` dynamically; the output node's measurement field is blank so it uses that value. Scalar numeric payloads are written to the InfluxDB field `value`. No tags are added by this flow. The InfluxDB 2.x output precision is configured as milliseconds (`ms`), but the flow sends no explicit timestamp, so points use their write time rather than a Volvo event timestamp.

| MQTT topic | Influx measurement | Stored field and format |
|---|---|---|
| `volvo/vehicle/sensor/battery_level` | `mcmdhome/volvo/battery_level` | `value`: number, battery percentage |
| `volvo/vehicle/sensor/electric_range` | `mcmdhome/volvo/range` | `value`: number; the original topic is renamed from `electric_range` to `range` |
| `volvo/vehicle/sensor/fuel_level` | `mcmdhome/volvo/fuel_level` | `value`: number |
| `volvo/vehicle/sensor/odometer` | `mcmdhome/volvo/odometer` | `value`: number |
| `volvo/vehicle/sensor/charging_power` | `mcmdhome/volvo/charging_power` | `value`: number |
| `volvo/vehicle/sensor/time_to_target_min` | `mcmdhome/volvo/time_to_target_min` | `value`: number, minutes |
| `volvo/vehicle/sensor/target_battery_level` | `mcmdhome/volvo/target_battery_level` | `value`: number, percentage |
| `volvo/vehicle/sensor/any_open` | `mcmdhome/volvo/any_open` | `value`: boolean (`true`/`false`) |

Empty or non-numeric readings are discarded, not stored as zero. The bridge also publishes unit topics such as `range_unit`, `fuel_unit`, and `odometer_unit`, but this flow ignores them; units are not stored as Influx fields or tags. Check the vehicle/API units before setting Grafana display units.

## Location measurement

The location InfluxDB output uses the `locations/autogen` bucket and writes to measurement `mcmdhome/gps/volvo`.

| Field | Influx type | Source |
|---|---|---|
| `longitude` | number | Coordinate index 0 |
| `latitude` | number | Coordinate index 1 |
| `heading` | number, when present | `properties.heading` |

These are fields on the same measurement and timestamp, not separate measurements. Altitude is deliberately omitted.

## MQTT-only output and ignored topics

When `volvo/vehicle/sensor/lock_status` is `LOCKED` or `UNLOCKED`, the flow publishes retained text to `mcmdhome/volvo/status`:

| Lock payload | MQTT output payload |
|---|---|
| `LOCKED` | `Secured` |
| `UNLOCKED` | `Unsecured` |

Lock status is not written to InfluxDB. The flow also ignores other sensor topics, including `charging_status`, `charger_connection`, `charging`, `plugged_in`, `range_unit`, and the standalone `heading` topic. Location heading is stored from the location JSON instead.

## Grafana Flux examples

A time-series query for odometer readings:

```flux
from(bucket: "environmentals/autogen")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "mcmdhome/volvo/odometer")
  |> filter(fn: (r) => r._field == "value")
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
  |> yield(name: "odometer")
```

For another numeric sensor, change `_measurement` to one of the environmentals measurements above. The field filter remains `_field == "value"`.

A location query that pivots latitude and longitude into columns for a Grafana Geomap/table visualization:

```flux
from(bucket: "locations/autogen")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "mcmdhome/gps/volvo")
  |> filter(fn: (r) => r._field == "latitude" or r._field == "longitude" or r._field == "heading")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) => exists r.latitude and exists r.longitude)
  |> keep(columns: ["_time", "latitude", "longitude", "heading"])
```

For a battery gauge or door-open state, select the matching measurement and `_field == "value"`. The door-open value is boolean; it is not a numeric 0/1 series.
