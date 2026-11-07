#!/bin/bash
# Temperature and GPU-text checks for the EC 3.5.8 ramp.
# This file does not insmod, write a fan, or read a default hardware path.
# The launcher passes /sys/class/thermal and the nvidia-smi text explicitly.
zones_ok() {
  local root="$1" z t ty name frac
  ZONE_LINE=""
  HOTTEST=""
  local hottest_t="" hottest_name=""
  if [ -z "$root" ] || [ ! -d "$root" ]; then
    echo "STOP_TEMP no_zones ${root:-empty}"
    return 1
  fi
  for z in "$root"/thermal_zone*; do
    name=${z##*/}
    if [ "$name" = "thermal_zone*" ]; then
      echo "STOP_TEMP no_zones $root"
      return 1
    fi
    if ! ty=$(cat "$z/type" 2>/dev/null); then
      echo "STOP_TEMP unreadable_type $z"
      return 1
    fi
    if ! t=$(cat "$z/temp" 2>/dev/null); then
      echo "STOP_TEMP unreadable $z type=$ty"
      return 1
    fi
    if ! [[ "$t" =~ ^-?[0-9]+$ ]]; then
      echo "STOP_TEMP nonnumeric $z type=$ty raw=$t"
      return 1
    fi
    if [ "$t" -lt 25000 ] || [ "$t" -gt 60000 ]; then
      echo "STOP_TEMP out_of_range $z type=$ty raw=${t}mC"
      return 1
    fi
    ZONE_LINE="$ZONE_LINE ${name}:${ty}=${t}mC"
    if [ -z "$hottest_t" ] || [ "$t" -gt "$hottest_t" ]; then
      hottest_t=$t
      hottest_name="${name}:${ty}"
    fi
  done
  frac=$(printf '%03d' $((hottest_t % 1000)))
  HOTTEST="${hottest_name}=${hottest_t}mC ($((hottest_t / 1000)).${frac}C)"
}
gpu_parse() {
  local utils="$1" util gtemp
  GPU_LINE=""
  if [ -z "$utils" ]; then
    echo "STOP_GPU empty"
    return 1
  fi
  while IFS=',' read -r util gtemp; do
    util=${util// /}
    gtemp=${gtemp// /}
    if [ "$util" != 0 ]; then
      echo "STOP_GPU util=$util nvidia_gpu_temp=${gtemp}C raw=$utils"
      return 1
    fi
    GPU_LINE="$GPU_LINE nvidia_gpu_temp=${gtemp}C"
  done <<< "$utils"
}
