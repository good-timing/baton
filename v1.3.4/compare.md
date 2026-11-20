## python: 1.3.3 published vs 1.3.4 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. The exception is a measure's 'limits': over 'repeats' runs of that measure alone, each named metric's median must be under its limit. scripts/check_bench_limit.py runs and checks them. A measure's 'noise_floor' gives, per metric and SDK, the change between runs of one version that is still noise. Median (lowest–highest) of 5, 5 runs.

CPU: 1.3.3 published: AMD EPYC 9V74 80-Core Processor (5); 1.3.4 checkout: AMD EPYC 9V74 80-Core Processor (5)

| measure | metric | 1.3.3 published | 1.3.4 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.33 (0.326–0.334) | 0.328 (0.323–0.346) | -1% (noise) |
| overhead | plain_p99_ms | 0.393 (0.39–0.413) | 0.392 (0.368–0.642) | -0% (noise) |
| overhead | baton_p50_ms | 0.654 (0.637–0.775) | 0.658 (0.653–0.751) | +1% (noise) |
| overhead | baton_p99_ms | 1.15 (1.14–1.32) | 1.19 (1.17–1.75) | +3% (noise) |
| overhead | added_p50_ms | 0.32 (0.307–0.448) | 0.335 (0.306–0.423) | +5% (noise) |
| overhead | added_p99_ms | 0.762 (0.746–0.902) | 0.82 (0.541–1.37) | +8% (noise) |
| burst | plain_wall_ms | 3,594 (3,530–3,709) | 3,582 (3,542–3,605) | -0% (noise) |
| burst | plain_call_p50_ms | 3.31 (3.25–3.36) | 3.29 (3.27–3.33) | -0% (noise) |
| burst | plain_call_p99_ms | 9.41 (9.04–9.52) | 9.06 (8.96–9.41) | -4% (noise) |
| burst | wall_ms | 6,880 (6,821–7,078) | 6,830 (6,712–6,990) | -1% (noise) |
| burst | call_p50_ms | 6.82 (6.79–7.01) | 6.78 (6.67–6.93) | -1% |
| burst | call_p99_ms | 9.3 (9.03–9.59) | 8.97 (8.87–9.27) | -4% |
| burst | added_call_p50_ms | 3.54 (3.48–3.65) | 3.5 (3.4–3.59) | -1% (noise) |
| burst | added_call_p99_ms | -0.0505 (-0.484–0.259) | -0.0511 (-0.388–0.116) | -0.000554 (noise) |
| burst | close_ms | 1.95 (1.69–2.81) | 2.04 (1.32–2.43) | +5% (noise) |
| burst | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | delivered | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | plain_wall_ms | 3,599 (3,519–3,637) | 3,585 (3,534–3,658) | -0% (noise) |
| outage_hang | plain_call_p50_ms | 3.31 (3.25–3.34) | 3.29 (3.27–3.36) | -1% (noise) |
| outage_hang | plain_call_p99_ms | 9.12 (9.01–9.28) | 9.17 (9.05–9.43) | +1% (noise) |
| outage_hang | wall_ms | 5,678 (5,504–5,741) | 5,608 (5,539–5,656) | -1% (noise) |
| outage_hang | call_p50_ms | 5.62 (5.44–5.63) | 5.52 (5.47–5.59) | -2% (noise) |
| outage_hang | call_p99_ms | 7.75 (6.95–8.01) | 7.25 (7.08–7.5) | -7% (noise) |
| outage_hang | added_call_p50_ms | 2.27 (2.19–2.32) | 2.21 (2.2–2.29) | -3% (noise) |
| outage_hang | added_call_p99_ms | -1.53 (-2.17–-1) | -1.95 (-2.19–-1.63) | -28% (noise) |
| outage_hang | close_ms | 3,005 (3,005–3,005) | 3,005 (3,005–3,005) | +0% (noise) |
| outage_hang | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | plain_wall_ms | 3,623 (3,534–3,657) | 3,582 (3,546–3,596) | -1% (noise) |
| outage_down | plain_call_p50_ms | 3.34 (3.27–3.35) | 3.31 (3.27–3.32) | -1% (noise) |
| outage_down | plain_call_p99_ms | 9.28 (9.02–9.44) | 9.25 (8.93–9.3) | -0% (noise) |
| outage_down | wall_ms | 6,045 (5,902–6,046) | 6,003 (5,951–6,049) | -1% (noise) |
| outage_down | call_p50_ms | 6.01 (5.87–6.02) | 5.97 (5.93–5.99) | -1% (noise) |
| outage_down | call_p99_ms | 7.75 (7.55–7.9) | 7.78 (7.65–8.23) | +0% (noise) |
| outage_down | added_call_p50_ms | 2.67 (2.6–2.68) | 2.66 (2.64–2.69) | -0% (noise) |
| outage_down | added_call_p99_ms | -1.41 (-1.7–-1.27) | -1.15 (-1.65–-0.897) | +18% |
| outage_down | close_ms | 299 (292–438) | 344 (297–394) | +15% (noise) |
| outage_down | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| memory | heap_after_warmup_bytes | 615,285 (604,824–628,307) | 611,767 (610,494–615,725) | -1% (noise) |
| memory | heap_at_half_bytes | 627,903 (619,363–648,118) | 631,032 (624,368–638,390) | +0% (noise) |
| memory | heap_at_end_bytes | 628,889 (625,206–647,007) | 631,662 (629,667–641,058) | +0% (noise) |
| memory | growth_per_call_bytes | 0.0986 (-2.06–2.18) | 0.524 (-0.872–1.01) | +0.425 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,504,735 (1,500,289–1,505,425) | 1,505,862 (1,503,846–1,507,312) | +0% |
| memory_outage | heap_at_half_bytes | 3,545,298 (3,544,771–3,546,466) | 3,547,089 (3,546,706–3,548,326) | +0% |
| memory_outage | heap_at_end_bytes | 3,636,733 (3,636,635–3,638,000) | 3,638,463 (3,637,688–3,639,517) | +0% |
| memory_outage | growth_per_call_bytes | 9.15 (9.12–9.19) | 9.12 (9.05–9.2) | -0.0343 (noise) |
| large_result | plain_p50_ms | 0.289 (0.276–0.308) | 0.287 (0.281–0.308) | -1% (noise) |
| large_result | plain_p99_ms | 0.346 (0.339–0.422) | 0.343 (0.327–0.351) | -1% (noise) |
| large_result | baton_p50_ms | 0.697 (0.688–0.706) | 0.702 (0.699–0.798) | +1% (noise) |
| large_result | baton_p99_ms | 2.29 (2.2–2.48) | 2.3 (2.29–2.37) | +1% (noise) |
| large_result | added_p50_ms | 0.411 (0.38–0.417) | 0.414 (0.409–0.49) | +1% (noise) |
| large_result | added_p99_ms | 1.9 (1.85–2.14) | 1.96 (1.95–2.03) | +3% (noise) |
| large_result | heap_growth_bytes | 2,151,299 (2,147,721–2,160,064) | 2,152,445 (2,146,944–2,156,900) | +0% (noise) |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| serve_overhead | plain_p50_ms | 0.000271 (0.00027–0.00028) | 0.00028 (0.00027–0.00029) | +3% (noise) |
| serve_overhead | plain_p99_ms | 0.000401 (0.000391–0.000451) | 0.000401 (0.000391–0.00042) | -0% (noise) |
| serve_overhead | baton_p50_ms | 0.34 (0.337–0.345) | 0.34 (0.338–0.343) | -0% (noise) |
| serve_overhead | baton_p99_ms | 0.888 (0.811–1.24) | 0.811 (0.798–0.823) | -9% |
| serve_overhead | added_p50_ms | 0.34 (0.337–0.345) | 0.339 (0.338–0.343) | -0% (noise) |
| serve_overhead | added_p99_ms | 0.887 (0.811–1.24) | 0.811 (0.797–0.823) | -9% |
| process | max_rss_bytes | 90,730,496 (90,714,112–95,686,656) | 90,849,280 (90,685,440–95,432,704) | +0% (noise) |
