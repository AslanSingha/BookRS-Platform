#!/usr/bin/env bash
cd "$(dirname "$0")/.."
echo "== downloads ==";  ls -lh data/ol/*.gz 2>/dev/null | awk '{print "   "$5"  "$9}'; grep done: data/ol/download.log 2>/dev/null | sed 's/^/   /'
echo "== slice ==";      tail -2 data/ol/slice.log 2>/dev/null | sed 's/^/   /' || echo "   not started"
echo "== stage1 ==";     tail -3 data/ol/stage1.log 2>/dev/null | sed 's/^/   /'
echo "== unimarc =="; tail -2 data/ol/stage1-unimarc.log 2>/dev/null | sed "s/^/   /"
echo "== pmb ==";     tail -1 data/ol/pmb-probe.log 2>/dev/null | sed "s/^/   /"; tail -2 data/ol/stage1-pmb.log 2>/dev/null | sed "s/^/   /"
echo "== bookrs ==";     curl -s http://localhost:8000/health | python -c 'import json,sys; d=json.load(sys.stdin); print("   works=%s embeddings=%s unembedded=%s" % (d["works"], d["embeddings"], d["unembedded"]))' 2>/dev/null || echo "   api down"
