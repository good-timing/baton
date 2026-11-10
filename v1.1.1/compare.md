## python: 1.1.0 published vs 1.1.1 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. The exception is a measure's 'limits': over 'repeats' runs of that measure alone, each named metric's median must be under its limit. scripts/check_bench_limit.py runs and checks them. A measure's 'noise_floor' gives, per metric and SDK, the change between runs of one version that is still noise. Median (lowest–highest) of 5, 5 runs.

CPU: 1.1.0 published: INTEL(R) XEON(R) PLATINUM 8573C (5); 1.1.1 checkout: INTEL(R) XEON(R) PLATINUM 8573C (5)

| measure | metric | 1.1.0 published | 1.1.1 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.477 (0.467–0.502) | 0.479 (0.469–0.492) | +1% (noise) |
| overhead | plain_p99_ms | 0.564 (0.536–0.613) | 0.564 (0.537–0.604) | -0% (noise) |
| overhead | baton_p50_ms | 1.03 (1.02–1.07) | 1.04 (0.896–1.09) | +0% (noise) |
| overhead | baton_p99_ms | 1.71 (1.69–1.78) | 1.69 (1.59–1.78) | -1% (noise) |
| overhead | added_p50_ms | 0.55 (0.53–0.587) | 0.544 (0.417–0.622) | -1% (noise) |
| overhead | added_p99_ms | 1.16 (1.13–1.24) | 1.12 (1.03–1.24) | -3% |
| burst | wall_ms | 9,278 (9,165–9,533) | 9,282 (9,034–9,447) | +0% (noise) |
| burst | call_p50_ms | 9.04 (8.94–9.29) | 9.03 (8.8–9.2) | -0% (noise) |
| burst | call_p99_ms | 14.6 (14.3–14.8) | 14.4 (14.1–14.6) | -1% (noise) |
| burst | close_ms | 2.48 (1.72–2.79) | 1.84 (1.76–2.41) | -26% (noise) |
| burst | emitted | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | delivered | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | wall_ms | 8,147 (8,086–8,354) | 8,154 (8,067–8,176) | +0% (noise) |
| outage_hang | call_p50_ms | 7.88 (7.82–8.11) | 7.85 (7.78–7.92) | -0% (noise) |
| outage_hang | call_p99_ms | 13.5 (13.4–14) | 13.5 (13.3–14.3) | +0% (noise) |
| outage_hang | close_ms | 3,005 (3,005–3,005) | 3,005 (3,005–3,005) | -0% (noise) |
| outage_hang | emitted | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| outage_down | wall_ms | 8,369 (8,093–8,621) | 8,439 (8,245–8,569) | +1% (noise) |
| outage_down | call_p50_ms | 8.11 (7.87–8.31) | 8.19 (8.02–8.34) | +1% (noise) |
| outage_down | call_p99_ms | 13.6 (12.9–14.5) | 13.8 (13.3–14.1) | +1% (noise) |
| outage_down | close_ms | 707 (706–707) | 707 (706–707) | +0% (noise) |
| outage_down | emitted | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| memory | heap_after_warmup_bytes | 609,275 (607,567–615,157) | 609,253 (608,320–617,895) | -0% (noise) |
| memory | heap_at_half_bytes | 631,332 (624,982–645,700) | 630,030 (627,364–632,298) | -0% (noise) |
| memory | heap_at_end_bytes | 628,476 (625,846–640,610) | 637,842 (622,142–642,107) | +1% (noise) |
| memory | growth_per_call_bytes | 0.0212 (-1.72–0.928) | 0.864 (-0.868–1.47) | +0.843 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,508,013 (1,496,029–1,508,092) | 1,505,727 (1,499,397–1,508,384) | -0% (noise) |
| memory_outage | heap_at_half_bytes | 3,633,218 (3,621,296–3,634,257) | 3,631,472 (3,615,767–3,633,526) | -0% (noise) |
| memory_outage | heap_at_end_bytes | 3,544,123 (3,543,882–3,545,456) | 3,543,440 (3,538,685–3,545,136) | -0% |
| memory_outage | growth_per_call_bytes | -8.88 (-8.92–-7.74) | -8.77 (-8.95–-7.62) | +0.11 (noise) |
| large_result | plain_p50_ms | 0.42 (0.397–0.435) | 0.412 (0.399–0.435) | -2% (noise) |
| large_result | plain_p99_ms | 0.515 (0.473–0.554) | 0.492 (0.468–0.505) | -4% (noise) |
| large_result | baton_p50_ms | 0.989 (0.918–1.11) | 0.983 (0.918–1.06) | -1% (noise) |
| large_result | baton_p99_ms | 2.97 (2.82–3.3) | 3.04 (2.96–3.16) | +2% (noise) |
| large_result | added_p50_ms | 0.591 (0.498–0.671) | 0.567 (0.506–0.654) | -4% (noise) |
| large_result | added_p99_ms | 2.45 (2.34–2.74) | 2.56 (2.5–2.65) | +5% (noise) |
| large_result | heap_growth_bytes | 2,151,424 (2,140,350–2,159,564) | 2,156,531 (1,947,619–2,160,490) | +0% (noise) |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| process | max_rss_bytes | 88,760,320 (88,584,192–93,990,912) | 88,698,880 (88,592,384–92,835,840) | -0% (noise) |
