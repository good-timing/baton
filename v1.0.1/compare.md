## python: 1.0.0 published vs 1.0.1 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. The exception is a measure's 'limits': over 'repeats' runs of that measure alone, each named metric's median must be under its limit. scripts/check_bench_limit.py runs and checks them. Median (lowest–highest) of 5, 5 runs.

CPU: 1.0.0 published: AMD EPYC 7763 64-Core Processor (5); 1.0.1 checkout: AMD EPYC 7763 64-Core Processor (5)

| measure | metric | 1.0.0 published | 1.0.1 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.549 (0.53–0.555) | 0.534 (0.529–0.544) | -3% (noise) |
| overhead | plain_p99_ms | 0.645 (0.603–0.687) | 0.608 (0.595–0.627) | -6% (noise) |
| overhead | baton_p50_ms | 0.992 (0.975–1.01) | 0.989 (0.968–0.995) | -0% (noise) |
| overhead | baton_p99_ms | 1.62 (1.6–1.72) | 1.63 (1.56–1.71) | +1% (noise) |
| overhead | added_p50_ms | 0.451 (0.437–0.457) | 0.444 (0.428–0.466) | -1% (noise) |
| overhead | added_p99_ms | 0.981 (0.957–1.1) | 1.02 (0.955–1.11) | +4% (noise) |
| burst | wall_ms | 9,797 (9,671–9,890) | 9,728 (9,657–9,953) | -1% (noise) |
| burst | call_p50_ms | 9.61 (9.55–9.69) | 9.55 (9.48–9.77) | -1% (noise) |
| burst | call_p99_ms | 13.8 (13.3–13.9) | 13.8 (13.5–14) | -0% (noise) |
| burst | close_ms | 1.53 (1.44–1.74) | 1.5 (1.35–1.62) | -2% (noise) |
| burst | emitted | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | delivered | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | wall_ms | 1,651 (1,619–1,658) | 1,631 (1,617–1,651) | -1% (noise) |
| outage_hang | call_p50_ms | 8.09 (7.95–8.11) | 7.99 (7.95–8.1) | -1% (noise) |
| outage_hang | call_p99_ms | 12.2 (11.7–12.5) | 11.9 (11.3–13.2) | -2% (noise) |
| outage_hang | close_ms | 3,005 (3,005–3,005) | 3,005 (3,005–3,005) | -0% (noise) |
| outage_hang | emitted | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_down | wall_ms | 1,733 (1,710–1,773) | 1,709 (1,690–1,746) | -1% |
| outage_down | call_p50_ms | 8.54 (8.44–8.71) | 8.41 (8.32–8.62) | -1% |
| outage_down | call_p99_ms | 12.8 (12.2–13.1) | 12.4 (11.9–13) | -3% (noise) |
| outage_down | close_ms | 706 (706–706) | 706 (706–706) | -0% (noise) |
| outage_down | emitted | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| memory | heap_after_warmup_bytes | 606,936 (602,489–629,633) | 609,424 (607,734–613,698) | +0% (noise) |
| memory | heap_at_half_bytes | 628,549 (617,540–640,084) | 627,917 (624,444–641,750) | -0% (noise) |
| memory | heap_at_end_bytes | 629,422 (623,225–640,363) | 630,410 (625,308–639,845) | +0% (noise) |
| memory | growth_per_call_bytes | 0.0279 (-0.836–1.62) | 0.01 (-0.866–0.597) | -0.0179 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,501,665 (1,497,672–1,503,009) | 1,504,541 (1,502,721–1,504,937) | +0% |
| memory_outage | heap_at_half_bytes | 3,618,612 (3,616,492–3,631,749) | 3,631,458 (3,622,134–3,632,144) | +0% (noise) |
| memory_outage | heap_at_end_bytes | 3,541,958 (3,539,943–3,542,588) | 3,541,730 (3,541,166–3,542,414) | -0% (noise) |
| memory_outage | growth_per_call_bytes | -7.67 (-8.92–-7.51) | -8.97 (-9.06–-8.02) | -17% |
| large_result | plain_p50_ms | 0.478 (0.468–0.504) | 0.477 (0.461–0.49) | -0% (noise) |
| large_result | plain_p99_ms | 0.542 (0.533–0.555) | 0.551 (0.518–0.581) | +2% (noise) |
| large_result | baton_p50_ms | 1 (0.988–1.03) | 1.03 (0.997–1.13) | +3% (noise) |
| large_result | baton_p99_ms | 2.84 (2.71–2.96) | 2.8 (2.76–2.96) | -1% (noise) |
| large_result | added_p50_ms | 0.524 (0.495–0.556) | 0.546 (0.513–0.668) | +4% (noise) |
| large_result | added_p99_ms | 2.3 (2.17–2.41) | 2.28 (2.21–2.41) | -1% (noise) |
| large_result | heap_growth_bytes | 2,158,493 (2,153,697–2,159,346) | 2,151,998 (2,147,140–2,157,603) | -0% |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| process | max_rss_bytes | 88,969,216 (88,784,896–94,232,576) | 88,924,160 (88,666,112–92,950,528) | -0% (noise) |
