# Third-party data and software

RadarForge's own code is yours to use as you like. It bundles or derives data from:

* **US Census Bureau** 2010 cartographic county boundaries (500k) – public domain.
* **Natural Earth** (naturalearthdata.com) – state/province lines, coastlines, lakes, roads – public domain.
* **GeoNames** (geonames.org) city names/populations, via the `geonamescache` and `reverse_geocoder`
  packages – CC BY 4.0. Attribution: © GeoNames.
* **Radar site table** (`radarforge/data/sites.json`) derived from Supercell Wx
  (https://github.com/dpaulat/supercell-wx), MIT License, Copyright (c) 2021-2026 Dan Paulat:

  > Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
  > associated documentation files (the "Software"), to deal in the Software without restriction,
  > including without limitation the rights to use, copy, modify, merge, publish, distribute,
  > sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
  > furnished to do so, subject to the following conditions: The above copyright notice and this
  > permission notice shall be included in all copies or substantial portions of the Software.
  > THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

Runtime dependencies (installed from PyPI): PySide6 (LGPL-3.0), NumPy, SciPy, scikit-image (BSD),
MetPy (BSD-3, used for Level III decoding), PyOpenGL (BSD-style), requests (Apache-2.0).

Data services used at runtime: NOAA NEXRAD on AWS (Unidata-managed buckets), the NWS API
(api.weather.gov), and the Iowa Environmental Mesonet (historical warnings and storm reports).
