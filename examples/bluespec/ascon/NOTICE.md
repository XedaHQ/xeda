This example is adapted from the Ascon BSV sources in BlueLight by Kamyar Mohajerani.
Source checkout: https://github.com/kammoh/bluelight at commit
4b90d3d689969f9fd44919345d2a8a646a8e3639, path `Ascon/`. The port source was a local checkout
with edits that disabled Probe instrumentation; this example removes Probe entirely. See
LICENSE.md for the Apache License, Version 2.0.

The reference C implementations used for the test vectors are vendored in the same BlueLight
revision, under these paths:

- `Ascon/cref/crypto_aead/ascon128v12/ref/`
- `Ascon/cref/crypto_aead/ascon128av12/ref/`
- `Ascon/cref/crypto_hash/asconhashv12/ref/`
