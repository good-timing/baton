## python: 1.2.0 published vs 1.3.0 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. The exception is a measure's 'limits': over 'repeats' runs of that measure alone, each named metric's median must be under its limit. scripts/check_bench_limit.py runs and checks them. A measure's 'noise_floor' gives, per metric and SDK, the change between runs of one version that is still noise. Median (lowest–highest) of 5, 5 runs.

CPU: 1.2.0 published: AMD EPYC 7763 64-Core Processor (5); 1.3.0 checkout: AMD EPYC 7763 64-Core Processor (5)

| measure | metric | 1.2.0 published | 1.3.0 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.519 (0.511–0.554) | 0.514 (0.503–0.515) | -1% (noise) |
| overhead | plain_p99_ms | 0.598 (0.585–0.605) | 0.585 (0.565–0.618) | -2% (noise) |
| overhead | baton_p50_ms | 0.964 (0.96–1.07) | 0.974 (0.96–1.06) | +1% (noise) |
| overhead | baton_p99_ms | 1.55 (1.46–1.62) | 1.46 (1.44–1.7) | -6% |
| overhead | added_p50_ms | 0.445 (0.42–0.56) | 0.46 (0.445–0.559) | +3% (noise) |
| overhead | added_p99_ms | 0.942 (0.864–1.02) | 0.894 (0.844–1.12) | -5% (noise) |
| burst | plain_wall_ms | 5,438 (5,383–5,519) | 5,382 (5,371–5,511) | -1% |
| burst | plain_call_p50_ms | 4.98 (4.93–5.03) | 4.93 (4.88–5.02) | -1% |
| burst | plain_call_p99_ms | 14.6 (13.7–14.8) | 14.2 (13.7–14.7) | -2% (noise) |
| burst | wall_ms | 9,428 (9,385–9,539) | 9,502 (9,306–9,613) | +1% (noise) |
| burst | call_p50_ms | 9.26 (9.22–9.36) | 9.31 (9.13–9.42) | +0% (noise) |
| burst | call_p99_ms | 13.3 (13.3–13.4) | 13.5 (13.2–13.6) | +1% |
| burst | added_call_p50_ms | 4.3 (4.24–4.34) | 4.34 (4.25–4.4) | +1% |
| burst | added_call_p99_ms | -1.14 (-1.45–-0.37) | -0.703 (-1.22–-0.292) | +38% (noise) |
| burst | close_ms | 2.46 (2.33–2.64) | 2.63 (2.26–2.8) | +7% (noise) |
| burst | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | delivered | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | plain_wall_ms | 5,408 (5,371–5,444) | 5,392 (5,349–5,498) | -0% (noise) |
| outage_hang | plain_call_p50_ms | 4.95 (4.88–4.99) | 4.93 (4.88–5.02) | -0% (noise) |
| outage_hang | plain_call_p99_ms | 14.4 (14.3–14.8) | 14.2 (14.2–14.6) | -1% |
| outage_hang | wall_ms | 8,298 (8,203–8,463) | 8,228 (8,197–8,370) | -1% (noise) |
| outage_hang | call_p50_ms | 8.16 (8.07–8.3) | 8.1 (8.05–8.24) | -1% (noise) |
| outage_hang | call_p99_ms | 11.6 (11.4–12.5) | 11.6 (11.5–11.7) | -0% (noise) |
| outage_hang | added_call_p50_ms | 3.21 (3.11–3.31) | 3.21 (3.14–3.22) | -0% (noise) |
| outage_hang | added_call_p99_ms | -2.93 (-3.17–-1.82) | -2.7 (-3.04–-2.56) | +8% (noise) |
| outage_hang | close_ms | 3,005 (3,005–3,005) | 3,005 (3,005–3,005) | +0% (noise) |
| outage_hang | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | plain_wall_ms | 5,414 (5,377–5,521) | 5,406 (5,356–5,480) | -0% (noise) |
| outage_down | plain_call_p50_ms | 4.96 (4.93–4.99) | 4.95 (4.88–5.03) | -0% (noise) |
| outage_down | plain_call_p99_ms | 14.3 (14.1–15.3) | 14 (13.8–14.5) | -2% |
| outage_down | wall_ms | 8,475 (8,423–8,539) | 8,509 (8,422–8,732) | +0% (noise) |
| outage_down | call_p50_ms | 8.38 (8.32–8.44) | 8.41 (8.31–8.6) | +0% (noise) |
| outage_down | call_p99_ms | 11.7 (11.6–11.9) | 11.8 (11.8–12.4) | +1% (noise) |
| outage_down | added_call_p50_ms | 3.43 (3.37–3.51) | 3.48 (3.43–3.57) | +2% (noise) |
| outage_down | added_call_p99_ms | -2.55 (-3.61–-2.22) | -2.11 (-2.77–-1.67) | +17% |
| outage_down | close_ms | 706 (705–706) | 706 (705–706) | -0% (noise) |
| outage_down | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| memory | heap_after_warmup_bytes | 610,142 (602,021–630,210) | 608,012 (601,799–614,222) | -0% (noise) |
| memory | heap_at_half_bytes | 635,084 (618,601–640,389) | 635,645 (616,619–650,856) | +0% (noise) |
| memory | heap_at_end_bytes | 628,150 (618,849–632,428) | 629,962 (622,463–634,066) | +0% (noise) |
| memory | growth_per_call_bytes | -0.802 (-0.886–0.117) | -0.158 (-2.09–0.584) | +0.644 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,506,196 (1,503,497–1,507,060) | 1,504,490 (1,502,452–1,506,635) | -0% (noise) |
| memory_outage | heap_at_half_bytes | 3,633,460 (3,632,979–3,633,886) | 3,630,979 (3,630,546–3,631,474) | -0% |
| memory_outage | heap_at_end_bytes | 3,542,655 (3,542,271–3,543,681) | 3,540,675 (3,539,623–3,540,957) | -0% |
| memory_outage | growth_per_call_bytes | -9.04 (-9.09–-9.02) | -9.05 (-9.11–-8.97) | -0.0078 (noise) |
| large_result | plain_p50_ms | 0.46 (0.456–0.49) | 0.46 (0.458–0.463) | -0% (noise) |
| large_result | plain_p99_ms | 0.527 (0.522–0.563) | 0.525 (0.508–0.539) | -0% (noise) |
| large_result | baton_p50_ms | 0.999 (0.994–1.13) | 1 (0.995–1.02) | +0% (noise) |
| large_result | baton_p99_ms | 2.62 (2.59–2.84) | 2.62 (2.57–2.67) | +0% (noise) |
| large_result | added_p50_ms | 0.539 (0.508–0.676) | 0.542 (0.535–0.558) | +0% (noise) |
| large_result | added_p99_ms | 2.09 (2.03–2.32) | 2.09 (2.04–2.14) | -0% (noise) |
| large_result | heap_growth_bytes | 2,152,250 (2,149,135–2,158,208) | 2,154,768 (2,147,266–2,160,253) | +0% (noise) |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| serve_overhead | plain_p50_ms | 0.00038 (0.00037–0.000391) | 0.000371 (0.000361–0.000391) | -2% (noise) |
| serve_overhead | plain_p99_ms | 0.000571 (0.000471–0.000621) | 0.000481 (0.00046–0.000541) | -16% (noise) |
| serve_overhead | baton_p50_ms | 0.54 (0.535–0.554) | 0.541 (0.539–0.562) | +0% (noise) |
| serve_overhead | baton_p99_ms | 1.1 (1.06–1.2) | 1.09 (1.06–1.12) | -1% (noise) |
| serve_overhead | added_p50_ms | 0.54 (0.535–0.554) | 0.541 (0.539–0.562) | +0% (noise) |
| serve_overhead | added_p99_ms | 1.1 (1.06–1.2) | 1.09 (1.06–1.12) | -1% (noise) |
| process | max_rss_bytes | 90,591,232 (90,501,120–95,576,064) | 90,505,216 (90,378,240–94,752,768) | -0% (noise) |
