#!/bin/bash
set -e
if [ $1 ]; then
  ROOTDIR=${1}
  echo -e "\n$ROOTDIR"
else
  echo -e "\nUsage Instructions:"
  echo -e "\t $0 directory"
  exit 1
fi

echo -e "yarn install"
yarn install

mkdir $ROOTDIR
echo -e "TARchive node_modules directory"
ls -lah ${{ github.workspace }}
tar -cvzf $ROOTDIR/node-modules-$DATE.tgz \
  node_modules yarn.lock package* .yarnrc.yml \
  | tee node-modules-$DATE.tgz.txt

echo -e "TARchive yarn cache directory"
ls -lah ${{ github.workspace }}
tar -cvzf $ROOTDIR/yarn-cache-$DATE.tgz \
  .yarn yarn.lock package* .yarnrc.yml \
  | tee yarn-cache-$DATE.tgz.txt

echo -e "\nDirectory Contents"
ls -lAhS $ROOTDIR/*
echo -e "\nDirectory Size"
du -hd2 $ROOTDIR/*
echo -e "\nDirectory Tree" 
tree $ROOTDIR