#!/bin/bash
time curl -L -T $2 $1/big &
sleep 1
time curl -L -T $3 $1/small &
sleep 300
#time crl -L $1/big -o big &
#sleep 5
time curl -L $1/small -o small &
sleep 60

