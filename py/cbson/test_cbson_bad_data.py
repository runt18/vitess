#!/usr/bin/python2.4

import base64
import binascii
import random
import sys
import unittest

import cbson

# These values were generated by test_random_segfaults and used to
# crash the decoder, before we fixed the issues.
# Only base64 encoded so they aren't a million characters long and/or full
# of escape sequences.
KNOWN_BAD = ["VAAAAARBAEwAAAAQMAABAAAAEDEAAgAAABAyAAMAAAAQMwAEAAAAEDQABQAAAAU"
               "1AAEAAAAANgI2AAIAAAA3AAM3AA8AAAACQwADAAAARFMAAB0A",
             "VAAAAARBAEwAAAAQMAABAAAAEDEAAgAAABAyAAMAAADTMwAEAAAAEDQABQAAAAU"
               "1AAEAAAAANgI2AAIAAAA3AAM3AA8AAAACQwADAAAARFMAAAAA",
             "VAAAAARBAEwAAAAQMAABAAAAEDEAAgAAABAyAAMAAAAQMwAEAAAAEDQABQAAAAU"
               "1AAEAAAAANgI2AAIAAAA3AAM3AA8AAAACQ2gDAAAARFMAAAAA",
            ]

class CbsonTest(unittest.TestCase):
  def test_short_string_segfaults(self):
    a = cbson.dumps({"A": [1, 2, 3, 4, 5, "6", u"7", {"C": u"DS"}]})
    for i in range(len(a))[1:]:
      try:
        cbson.loads(a[:-i] + (" " * i))
      except Exception:
        pass

  def test_known_bad(self):
    for s in KNOWN_BAD:
      try:
        d = base64.b64decode(s)
        cbson.loads(d)
      except cbson.BSONError:
        pass

  def test_random_segfaults(self):
    a = cbson.dumps({"A": [1, 2, 3, 4, 5, "6", u"7", {"C": u"DS"}]})
    sys.stdout.write("\nQ: {0!s}\n".format(binascii.hexlify(a)))
    for i in range(1000):
      l = [c for c in a]
      l[random.randint(4, len(a)-1)] = chr(random.randint(0, 255))
      try:
        s = "".join(l)
        sys.stdout.write("s: {0!s}\n".format(binascii.hexlify(s)))
        sys.stdout.flush()
        cbson.loads(s)
      except Exception as e:
        sys.stdout.write("  ERROR: {0!r}\n".format(e))
        sys.stdout.flush()

if __name__ == "__main__":
  unittest.main()

