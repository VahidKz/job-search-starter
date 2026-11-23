# Job Radar Provenance

Upstream: https://github.com/maccydee/job-radar
Pinned revision: e108db875db11b526ef89be0c45330687097da69
Author: Callum McDonald
Licence: MIT (see LICENSE).

The implementation is installed into an ignored, isolated checkout by
`automation/job_radar.py install`. We call its collection and diagnostic APIs;
we do not maintain a second vendored application or duplicate application history.
The original licence also remains in that checkout.
