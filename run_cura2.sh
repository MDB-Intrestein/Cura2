#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Set up Qt-related ENV variables
if [ -e /opt/qt58/bin/qt58-env.sh ]; then
  source /opt/qt58/bin/qt58-env.sh
elif [ -e /opt/qt56/bin/qt56-env.sh ]; then
  source /opt/qt56/bin/qt56-env.sh
fi

# Set up custom python3 site packages related to Cura2
export PYTHONPATH=/opt/cura2/python3.5/site-packages

# Protobuf Custom location
if [ -z "$CURA2_LIBS" ]; then
  CURA2_LIBS="/opt/cura2/lib"
fi
if [ ! -z "$CURA2_LIBS" ]; then
  export LD_LIBRARY_PATH="$CURA2_LIBS:$LD_LIBRARY_PATH"
fi
# run Cura2

$DIR/cura_app.py
