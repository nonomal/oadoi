#!/bin/bash
# dyno number avail in $DYNO as per http://stackoverflow.com/questions/16372425/can-you-programmatically-access-current-heroku-dyno-id-name/16381078#16381078

for (( i=1; i<=$PDF_PARSE_N_WORKERS; i++ ))
do
  COMMAND="python3 pdf_parse.py -t 10"
  echo $COMMAND
  $COMMAND &
done
trap "kill 0" INT TERM EXIT
wait
