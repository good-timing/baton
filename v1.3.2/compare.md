## python: 1.3.1 published vs 1.3.2 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. The exception is a measure's 'limits': over 'repeats' runs of that measure alone, each named metric's median must be under its limit. scripts/check_bench_limit.py runs and checks them. A measure's 'noise_floor' gives, per metric and SDK, the change between runs of one version that is still noise. Median (lowest–highest) of 5, 5 runs.

CPU: 1.3.1 published: INTEL(R) XEON(R) PLATINUM 8573C (5); 1.3.2 checkout: INTEL(R) XEON(R) PLATINUM 8573C (5)

| measure | metric | 1.3.1 published | 1.3.2 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.466 (0.461–0.474) | 0.469 (0.467–0.496) | +1% (noise) |
| overhead | plain_p99_ms | 0.543 (0.531–0.583) | 0.563 (0.554–0.578) | +4% (noise) |
| overhead | baton_p50_ms | 0.963 (0.892–1.01) | 0.916 (0.905–1.04) | -5% (noise) |
| overhead | baton_p99_ms | 1.63 (1.57–1.68) | 1.63 (1.62–1.64) | +0% (noise) |
| overhead | added_p50_ms | 0.495 (0.426–0.544) | 0.448 (0.427–0.569) | -9% (noise) |
| overhead | added_p99_ms | 1.07 (1.04–1.11) | 1.07 (1.05–1.09) | +0% (noise) |
| burst | plain_wall_ms | 5,045 (4,962–5,175) | 4,995 (4,965–5,013) | -1% (noise) |
| burst | plain_call_p50_ms | 4.54 (4.48–4.71) | 4.52 (4.49–4.55) | -1% (noise) |
| burst | plain_call_p99_ms | 13.2 (13.1–13.4) | 13.1 (12.9–13.3) | -1% |
| burst | wall_ms | 9,093 (9,034–9,231) | 9,141 (9,015–9,206) | +1% (noise) |
| burst | call_p50_ms | 8.87 (8.83–9.02) | 8.94 (8.79–8.99) | +1% (noise) |
| burst | call_p99_ms | 14.1 (13.7–14.5) | 14 (13.8–14.5) | -0% (noise) |
| burst | added_call_p50_ms | 4.31 (4.29–4.39) | 4.44 (4.24–4.49) | +3% |
| burst | added_call_p99_ms | 0.688 (0.436–1.34) | 0.848 (0.7–1.51) | +23% (noise) |
| burst | close_ms | 2.74 (1.85–3.52) | 3.2 (2.56–3.8) | +17% (noise) |
| burst | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | delivered | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | plain_wall_ms | 5,029 (4,989–5,075) | 5,045 (4,956–5,077) | +0% (noise) |
| outage_hang | plain_call_p50_ms | 4.57 (4.53–4.61) | 4.57 (4.51–4.61) | -0% (noise) |
| outage_hang | plain_call_p99_ms | 13.3 (13–13.8) | 13 (12.7–13.5) | -3% (noise) |
| outage_hang | wall_ms | 7,955 (7,908–8,203) | 8,103 (8,043–8,137) | +2% (noise) |
| outage_hang | call_p50_ms | 7.72 (7.69–7.94) | 7.87 (7.82–7.93) | +2% (noise) |
| outage_hang | call_p99_ms | 13.1 (13–13.7) | 13.4 (13.1–13.7) | +2% (noise) |
| outage_hang | added_call_p50_ms | 3.17 (3.15–3.35) | 3.32 (3.26–3.36) | +5% (noise) |
| outage_hang | added_call_p99_ms | -0.127 (-0.534–0.113) | 0.162 (-0.117–0.747) | +0.289 |
| outage_hang | close_ms | 3,005 (3,005–3,005) | 3,005 (3,005–3,005) | +0% |
| outage_hang | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | plain_wall_ms | 5,024 (4,999–5,077) | 4,985 (4,970–5,087) | -1% |
| outage_down | plain_call_p50_ms | 4.54 (4.53–4.59) | 4.54 (4.49–4.66) | -0% (noise) |
| outage_down | plain_call_p99_ms | 13.2 (13.1–13.8) | 13 (12.9–13.6) | -1% |
| outage_down | wall_ms | 8,273 (8,173–8,335) | 8,283 (8,267–8,334) | +0% (noise) |
| outage_down | call_p50_ms | 8.06 (7.97–8.15) | 8.07 (8.04–8.15) | +0% (noise) |
| outage_down | call_p99_ms | 13.1 (13.1–13.4) | 13 (13–13.8) | -0% |
| outage_down | added_call_p50_ms | 3.48 (3.43–3.62) | 3.53 (3.49–3.57) | +2% (noise) |
| outage_down | added_call_p99_ms | -0.105 (-0.752–0.197) | 0.16 (-0.6–0.529) | +0.264 (noise) |
| outage_down | close_ms | 706 (706–707) | 706 (706–706) | -0% (noise) |
| outage_down | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| memory | heap_after_warmup_bytes | 615,974 (602,152–627,461) | 623,762 (606,297–625,034) | +1% (noise) |
| memory | heap_at_half_bytes | 622,172 (613,616–627,572) | 630,254 (621,605–639,033) | +1% |
| memory | heap_at_end_bytes | 623,331 (615,289–636,729) | 629,474 (619,234–648,007) | +1% (noise) |
| memory | growth_per_call_bytes | 0.167 (-0.859–1.45) | 0.0397 (-1.98–2.64) | -0.128 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,505,524 (1,502,990–1,506,911) | 1,504,497 (1,501,499–1,506,270) | -0% (noise) |
| memory_outage | heap_at_half_bytes | 3,631,409 (3,631,032–3,632,957) | 3,631,038 (3,629,567–3,636,719) | -0% (noise) |
| memory_outage | heap_at_end_bytes | 3,541,238 (3,540,413–3,541,965) | 3,540,595 (3,539,959–3,546,035) | -0% (noise) |
| memory_outage | growth_per_call_bytes | -9.06 (-9.16–-9.01) | -9.02 (-9.07–-8.95) | +0.0416 (noise) |
| large_result | plain_p50_ms | 0.418 (0.401–0.437) | 0.402 (0.394–0.435) | -4% (noise) |
| large_result | plain_p99_ms | 0.506 (0.47–0.517) | 0.512 (0.466–0.556) | +1% (noise) |
| large_result | baton_p50_ms | 0.937 (0.913–0.969) | 0.964 (0.948–1.03) | +3% (noise) |
| large_result | baton_p99_ms | 2.89 (2.74–3.37) | 2.75 (2.69–2.89) | -5% (noise) |
| large_result | added_p50_ms | 0.519 (0.507–0.549) | 0.563 (0.529–0.628) | +8% |
| large_result | added_p99_ms | 2.38 (2.23–2.88) | 2.24 (2.22–2.38) | -6% (noise) |
| large_result | heap_growth_bytes | 2,153,030 (2,148,152–2,159,171) | 2,158,924 (2,151,674–2,160,403) | +0% (noise) |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| serve_overhead | plain_p50_ms | 0.000315 (0.000307–0.000318) | 0.000296 (0.000293–0.000316) | -6% |
| serve_overhead | plain_p99_ms | 0.000482 (0.000445–0.000553) | 0.000523 (0.000507–0.000548) | +9% (noise) |
| serve_overhead | baton_p50_ms | 0.502 (0.48–0.517) | 0.498 (0.491–0.505) | -1% (noise) |
| serve_overhead | baton_p99_ms | 1.11 (1.05–1.15) | 1.09 (1.07–1.13) | -1% (noise) |
| serve_overhead | added_p50_ms | 0.502 (0.48–0.517) | 0.497 (0.49–0.504) | -1% (noise) |
| serve_overhead | added_p99_ms | 1.11 (1.05–1.15) | 1.09 (1.06–1.13) | -1% (noise) |
| process | max_rss_bytes | 90,304,512 (90,202,112–95,133,696) | 90,218,496 (90,021,888–94,924,800) | -0% (noise) |
