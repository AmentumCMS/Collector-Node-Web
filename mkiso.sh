#!/bin/bash
set -e
if [ $1 ]; then
  ROOTDIR=$1
  echo -e "\n$ROOTDIR"
else
  echo -e "\nUsage Instructions:"
  echo -e "\t $0 directory"
  echo -e "\nUsing default
  ROOTDIR=${basename $PWD}
  echo -e "\n$ROOTDIR"
fi

if [ -n $DATE ]; then
  DATE=`date '+%Y%m%d-%H%M'`
fi

echo "Run Collection Script for $ROOTDIR.txt"
./collect.sh $ROOTDIR.txt
  
echo "Copy README to ISO"
cp -v README.md $ROOTDIR/

echo "Make ISO"
echo -e "\nMaking ISO of $1"
mkisofs -J -R -v -T -l \
  -V $ROOTDIR-$DATE \
  -A $ROOTDIR-$DATE \
  -o $ROOTDIR-$DATE.iso \
  $ROOTDIR

echo "Implant MD5 sum into iso"
implantisomd5 $ROOTDIR-$DATE.iso

echo "Generate Hash"
sha256sum -b $ROOTDIR-$DATE.iso >>\
  $ROOTDIR-$DATE.iso.sha
cat $ROOTDIR-$DATE.iso.sha

echo "Show ISO & Hash"
ls -Alht *.iso*