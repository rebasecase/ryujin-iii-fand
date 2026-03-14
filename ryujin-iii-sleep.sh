#!/bin/bash
# Sends SIGUSR1/SIGUSR2 to ryujin-iii-fand on sleep/wake

case "$1" in
    pre)
        pkill -USR1 -f ryujin_iii_fand
        ;;
    post)
        pkill -USR2 -f ryujin_iii_fand
        ;;
esac
