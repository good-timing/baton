## python: 0.9.7 published vs 1.0.0 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. Median (lowest–highest) of 5, 5 runs.

| measure | metric | 0.9.7 published | 1.0.0 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.349 (0.333–0.354) | 0.349 (0.341–0.37) | -0% (noise) |
| overhead | plain_p99_ms | 0.569 (0.368–0.78) | 0.524 (0.379–0.773) | -8% (noise) |
| overhead | baton_p50_ms | 0.662 (0.637–0.674) | 0.647 (0.614–0.683) | -2% (noise) |
| overhead | baton_p99_ms | 1.12 (1.05–1.21) | 1.1 (1.09–1.12) | -2% (noise) |
| overhead | added_p50_ms | 0.315 (0.283–0.329) | 0.299 (0.272–0.334) | -5% (noise) |
| overhead | added_p99_ms | 0.531 (0.273–0.838) | 0.572 (0.316–0.717) | +8% (noise) |
| burst | wall_ms | 6,166 (6,140–6,362) | 6,134 (6,117–6,219) | -1% |
| burst | call_p50_ms | 5.97 (5.95–6.19) | 5.95 (5.92–6.02) | -0% |
| burst | call_p99_ms | 10.1 (9.84–10.2) | 9.88 (9.71–9.94) | -2% (noise) |
| burst | close_ms | 1.48 (1.17–1.63) | 1.66 (1.29–1.78) | +12% |
| burst | emitted | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | delivered | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | wall_ms | 1,066 (1,054–1,108) | 1,074 (1,062–1,096) | +1% (noise) |
| outage_hang | call_p50_ms | 5.14 (5.07–5.36) | 5.12 (5.06–5.22) | -0% (noise) |
| outage_hang | call_p99_ms | 9.23 (9.06–9.51) | 9.37 (8.86–10.1) | +2% (noise) |
| outage_hang | close_ms | 3,005 (3,003–3,005) | 3,005 (3,005–3,005) | +0% (noise) |
| outage_hang | emitted | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_down | wall_ms | 1,069 (1,058–1,130) | 1,074 (1,068–1,093) | +0% (noise) |
| outage_down | call_p50_ms | 5.15 (5.11–5.42) | 5.18 (5.14–5.31) | +1% (noise) |
| outage_down | call_p99_ms | 9.07 (8.81–9.97) | 9.1 (8.82–9.12) | +0% (noise) |
| outage_down | close_ms | 706 (705–706) | 706 (706–706) | +0% (noise) |
| outage_down | emitted | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| memory | heap_after_warmup_bytes | 615,772 (609,902–627,501) | 606,857 (524,975–614,915) | -1% |
| memory | heap_at_half_bytes | 553,895 (548,729–626,121) | 641,600 (550,120–653,081) | +16% |
| memory | heap_at_end_bytes | 553,675 (549,463–648,117) | 576,922 (546,905–647,477) | +4% (noise) |
| memory | growth_per_call_bytes | 0.103 (-7.67–9.94) | -7.62 (-9.83–7.73) | -7.72 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,504,602 (1,501,844–1,506,298) | 1,502,015 (1,498,491–1,504,898) | -0% (noise) |
| memory_outage | heap_at_half_bytes | 3,547,937 (3,546,745–3,548,435) | 3,544,963 (3,542,152–3,545,239) | -0% |
| memory_outage | heap_at_end_bytes | 3,548,653 (3,547,266–3,639,693) | 3,546,113 (3,545,832–3,546,869) | -0% |
| memory_outage | growth_per_call_bytes | 0.0496 (-0.0048–9.29) | 0.161 (0.103–0.368) | +0.111 (noise) |
| large_result | plain_p50_ms | 0.294 (0.28–0.329) | 0.299 (0.291–0.301) | +2% (noise) |
| large_result | plain_p99_ms | 0.402 (0.312–0.823) | 0.333 (0.323–0.651) | -17% (noise) |
| large_result | baton_p50_ms | 0.682 (0.674–0.711) | 0.66 (0.644–0.685) | -3% |
| large_result | baton_p99_ms | 1.93 (1.89–2) | 1.91 (1.87–1.92) | -1% (noise) |
| large_result | added_p50_ms | 0.394 (0.353–0.406) | 0.369 (0.343–0.385) | -6% (noise) |
| large_result | added_p99_ms | 1.53 (1.06–1.69) | 1.57 (1.26–1.58) | +3% (noise) |
| large_result | heap_growth_bytes | 2,157,658 (2,106,381–2,912,210) | 2,150,606 (2,106,161–2,152,530) | -0% (noise) |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| process | max_rss_bytes | 85,286,912 (84,713,472–89,985,024) | 85,327,872 (84,783,104–89,886,720) | +0% (noise) |
