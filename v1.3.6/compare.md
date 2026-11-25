## python: 1.3.5 published vs 1.3.6 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. The exception is a measure's 'limits': over 'repeats' runs of that measure alone, each named metric's median must be under its limit. scripts/check_bench_limit.py runs and checks them. A measure's 'noise_floor' gives, per metric and SDK, the change between runs of one version that is still noise. Median (lowest–highest) of 5, 5 runs.

CPU: 1.3.5 published: AMD EPYC 7763 64-Core Processor (5); 1.3.6 checkout: AMD EPYC 7763 64-Core Processor (5)

| measure | metric | 1.3.5 published | 1.3.6 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.519 (0.509–0.531) | 0.525 (0.506–0.544) | +1% (noise) |
| overhead | plain_p99_ms | 0.597 (0.561–0.651) | 0.615 (0.57–0.648) | +3% (noise) |
| overhead | baton_p50_ms | 1.08 (1.01–1.1) | 0.978 (0.972–0.992) | -10% |
| overhead | baton_p99_ms | 1.54 (1.48–1.66) | 1.52 (1.48–1.6) | -1% (noise) |
| overhead | added_p50_ms | 0.564 (0.497–0.566) | 0.456 (0.433–0.466) | -19% |
| overhead | added_p99_ms | 0.948 (0.885–1.06) | 0.946 (0.845–0.987) | -0% (noise) |
| burst | plain_wall_ms | 5,479 (5,425–5,718) | 5,513 (5,400–5,539) | +1% (noise) |
| burst | plain_call_p50_ms | 5.01 (4.93–5.18) | 4.97 (4.9–5.05) | -1% (noise) |
| burst | plain_call_p99_ms | 14.4 (14–15) | 14.6 (14.1–15.2) | +2% (noise) |
| burst | wall_ms | 9,543 (9,367–9,697) | 9,526 (9,494–9,646) | -0% (noise) |
| burst | call_p50_ms | 9.38 (9.25–9.54) | 9.39 (9.34–9.51) | +0% (noise) |
| burst | call_p99_ms | 13.1 (12.9–13.4) | 13.1 (13.1–13.2) | +0% (noise) |
| burst | added_call_p50_ms | 4.37 (4.31–4.42) | 4.44 (4.38–4.49) | +2% |
| burst | added_call_p99_ms | -1.48 (-1.72–-0.796) | -1.54 (-2.06–-0.996) | -4% (noise) |
| burst | close_ms | 2.45 (1.59–2.78) | 2.57 (2.36–2.76) | +5% (noise) |
| burst | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | delivered | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | plain_wall_ms | 5,441 (5,420–5,603) | 5,490 (5,388–5,561) | +1% (noise) |
| outage_hang | plain_call_p50_ms | 4.95 (4.93–5.09) | 4.97 (4.9–5.08) | +1% (noise) |
| outage_hang | plain_call_p99_ms | 14.6 (14.1–14.8) | 14.4 (14.1–15.2) | -2% (noise) |
| outage_hang | wall_ms | 8,374 (8,239–8,503) | 8,373 (8,292–8,477) | -0% (noise) |
| outage_hang | call_p50_ms | 8.25 (8.12–8.39) | 8.28 (8.17–8.35) | +0% (noise) |
| outage_hang | call_p99_ms | 11.7 (11.6–11.8) | 11.5 (11.5–11.8) | -1% |
| outage_hang | added_call_p50_ms | 3.3 (3.12–3.35) | 3.24 (3.22–3.39) | -2% (noise) |
| outage_hang | added_call_p99_ms | -2.93 (-3.16–-2.45) | -2.72 (-3.68–-2.54) | +7% (noise) |
| outage_hang | close_ms | 3,005 (3,005–3,005) | 3,005 (3,005–3,005) | +0% (noise) |
| outage_hang | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | plain_wall_ms | 5,439 (5,379–5,541) | 5,465 (5,410–5,620) | +0% (noise) |
| outage_down | plain_call_p50_ms | 4.94 (4.92–5.03) | 4.96 (4.92–5.09) | +0% (noise) |
| outage_down | plain_call_p99_ms | 14.4 (14.1–14.7) | 14.5 (14.3–14.9) | +1% (noise) |
| outage_down | wall_ms | 8,615 (8,529–8,808) | 8,576 (8,566–8,682) | -0% (noise) |
| outage_down | call_p50_ms | 8.52 (8.44–8.71) | 8.49 (8.48–8.6) | -0% (noise) |
| outage_down | call_p99_ms | 11.7 (11.7–12.1) | 11.8 (11.7–11.9) | +1% (noise) |
| outage_down | added_call_p50_ms | 3.58 (3.51–3.76) | 3.58 (3.4–3.64) | -0% (noise) |
| outage_down | added_call_p99_ms | -2.62 (-3.04–-2.15) | -2.8 (-3.15–-2.35) | -7% (noise) |
| outage_down | close_ms | 706 (705–706) | 706 (705–706) | +0% (noise) |
| outage_down | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| memory | heap_after_warmup_bytes | 615,040 (609,708–625,577) | 610,870 (604,774–619,541) | -1% (noise) |
| memory | heap_at_half_bytes | 631,430 (625,817–631,520) | 629,263 (622,440–635,657) | -0% (noise) |
| memory | heap_at_end_bytes | 640,724 (626,405–641,070) | 630,942 (549,433–637,892) | -2% (noise) |
| memory | growth_per_call_bytes | 0.931 (0.0588–1.01) | 0.0684 (-7.3–0.863) | -0.862 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,506,432 (1,506,235–1,506,830) | 1,508,367 (1,504,480–1,510,553) | +0% |
| memory_outage | heap_at_half_bytes | 3,631,487 (3,618,354–3,633,242) | 3,635,210 (3,623,316–3,636,766) | +0% |
| memory_outage | heap_at_end_bytes | 3,542,616 (3,540,682–3,542,839) | 3,543,894 (3,543,853–3,544,550) | +0% |
| memory_outage | growth_per_call_bytes | -8.95 (-9.08–-7.55) | -9.07 (-9.29–-7.94) | -0.121 (noise) |
| large_result | plain_p50_ms | 0.487 (0.459–0.503) | 0.47 (0.45–0.48) | -3% (noise) |
| large_result | plain_p99_ms | 0.587 (0.516–0.622) | 0.539 (0.505–0.737) | -8% (noise) |
| large_result | baton_p50_ms | 1.03 (1.02–1.05) | 1.03 (0.997–1.14) | -0% (noise) |
| large_result | baton_p99_ms | 2.87 (2.65–3.34) | 2.69 (2.58–3.1) | -6% (noise) |
| large_result | added_p50_ms | 0.537 (0.526–0.585) | 0.56 (0.534–0.69) | +4% (noise) |
| large_result | added_p99_ms | 2.3 (2.13–2.71) | 2.1 (2–2.59) | -9% |
| large_result | heap_growth_bytes | 2,152,125 (2,150,552–2,159,056) | 2,158,070 (2,145,157–2,160,252) | +0% (noise) |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| serve_overhead | plain_p50_ms | 0.00038 (0.000361–0.000391) | 0.00037 (0.000361–0.000401) | -3% (noise) |
| serve_overhead | plain_p99_ms | 0.000481 (0.000461–0.000641) | 0.000501 (0.000471–0.000601) | +4% (noise) |
| serve_overhead | baton_p50_ms | 0.555 (0.542–0.582) | 0.553 (0.544–0.557) | -0% (noise) |
| serve_overhead | baton_p99_ms | 1.13 (1.06–1.21) | 1.11 (1.08–1.21) | -1% (noise) |
| serve_overhead | added_p50_ms | 0.555 (0.542–0.581) | 0.553 (0.543–0.557) | -0% (noise) |
| serve_overhead | added_p99_ms | 1.13 (1.06–1.21) | 1.11 (1.08–1.21) | -1% (noise) |
| process | max_rss_bytes | 91,111,424 (91,033,600–96,051,200) | 91,385,856 (90,812,416–95,182,848) | +0% (noise) |
