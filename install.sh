#!/usr/bin/env bash

if [ -e python-kasa ]; then
  skip_rm=0
  if [ -f .dev_python_kasa.json ]; then
    skip_rm=$(python3 -c "import json; print(1 if json.load(open('.dev_python_kasa.json')).get('enabled') else 0)" 2>/dev/null || echo 0)
  fi
  if [ "$skip_rm" = "1" ]; then
    echo "Keeping python-kasa (dev_python_kasa enabled)"
  else
    echo "Removing python-kasa..."
    rm -rf python-kasa
  fi
fi

#echo ""
#if [ -e python-kasa ]; then
#  echo "Updating python-kasa..."
#  cd python-kasa
#  git pull
#  cd ..
#else
#  git clone https://github.com/jimboca/python-kasa.git
#fi

#repo=pyHS100
#if [ -e $repo ]; then
#  echo "Updating $repo ..."
#  cd $repo
#  git pull
#  cd ..
#else
#  git clone https://github.com/jimboca/$repo
#fi

if [  $# -gt 0 ]; then
  echo "Skipping pip3 install, must be a travis run?"
else
  pip3 install --upgrade pip
  pip3 install -r requirements.txt --user --no-warn-script-location --upgrade
fi
