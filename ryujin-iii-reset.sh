#!/bin/bash
# Virtual USB replug for Ryujin III — no physical cable needed
# Usage: sudo ./ryujin-reset.sh

DEV=$(grep -rl "1ada" /sys/bus/usb/devices/*/idProduct 2>/dev/null | head -1 | sed 's|/idProduct||;s|.*/||')

if [ -z "$DEV" ]; then
    echo "Ryujin III not found"
    exit 1
fi

echo "Resetting USB device $DEV..."
echo "$DEV" > /sys/bus/usb/drivers/usb/unbind
sleep 1
echo "$DEV" > /sys/bus/usb/drivers/usb/bind
echo "Done"
