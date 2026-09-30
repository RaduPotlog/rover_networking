<p align="center">
  <img src="rutx11/icons/Logo-Arm-WhiteOrange-372x372-1.png" alt="Mechatronics Academy" width="140">
</p>

# rover_networking

Networking for the Rover A1. The repo sits in the ROS workspace's `src/`; nothing in it is a ROS
package, so `COLCON_IGNORE` keeps colcon away from it.

| Folder | What it is |
|--------|------------|
| [`rutx11/`](rutx11/README.md) | The **Teltonika RUTX11** router: its network layout, and the web page that switches the router's Wi-Fi uplink without breaking the firewall/NAT rules (`uplink_manager`). Runs on the rover as the container `rover-a1-network` of [rover_docker](https://github.com/RaduPotlog/rover_docker), or on a laptop with `rutx11/run-local.sh`. |
