## python: 1.3.4 published vs 1.3.5 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. The exception is a measure's 'limits': over 'repeats' runs of that measure alone, each named metric's median must be under its limit. scripts/check_bench_limit.py runs and checks them. A measure's 'noise_floor' gives, per metric and SDK, the change between runs of one version that is still noise. Median (lowest–highest) of 5, 5 runs.

CPU: 1.3.4 published: AMD EPYC 9V45 96-Core Processor (5); 1.3.5 checkout: AMD EPYC 9V45 96-Core Processor (5)

| measure | metric | 1.3.4 published | 1.3.5 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.208 (0.208–0.211) | 0.215 (0.209–0.218) | +3% |
| overhead | plain_p99_ms | 0.24 (0.234–0.251) | 0.248 (0.245–0.586) | +3% (noise) |
| overhead | baton_p50_ms | 0.417 (0.399–0.454) | 0.4 (0.394–0.438) | -4% (noise) |
| overhead | baton_p99_ms | 0.72 (0.704–0.726) | 0.717 (0.663–0.741) | -0% (noise) |
| overhead | added_p50_ms | 0.207 (0.191–0.242) | 0.187 (0.179–0.221) | -10% |
| overhead | added_p99_ms | 0.48 (0.464–0.488) | 0.47 (0.108–0.491) | -2% (noise) |
| burst | plain_wall_ms | 2,307 (2,292–2,365) | 2,342 (2,296–2,557) | +2% (noise) |
| burst | plain_call_p50_ms | 2.13 (2.11–2.19) | 2.17 (2.12–2.23) | +2% (noise) |
| burst | plain_call_p99_ms | 6.12 (5.95–6.24) | 6.34 (6.06–8.87) | +3% |
| burst | wall_ms | 4,236 (4,202–4,293) | 4,238 (4,184–4,303) | +0% (noise) |
| burst | call_p50_ms | 4.12 (4.08–4.16) | 4.11 (4.05–4.14) | -0% (noise) |
| burst | call_p99_ms | 6.56 (6.51–6.92) | 6.75 (6.55–8.46) | +3% (noise) |
| burst | added_call_p50_ms | 1.99 (1.95–2.01) | 1.93 (1.88–1.97) | -3% |
| burst | added_call_p99_ms | 0.613 (0.359–0.682) | 0.584 (-0.403–0.612) | -5% (noise) |
| burst | close_ms | 1.44 (1.03–1.52) | 1.45 (1.38–1.52) | +1% (noise) |
| burst | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | delivered | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | plain_wall_ms | 2,312 (2,291–2,341) | 2,323 (2,289–2,513) | +0% (noise) |
| outage_hang | plain_call_p50_ms | 2.12 (2.11–2.14) | 2.15 (2.1–2.21) | +1% |
| outage_hang | plain_call_p99_ms | 6.18 (5.83–6.35) | 6.11 (5.92–8.71) | -1% (noise) |
| outage_hang | wall_ms | 3,539 (3,478–3,584) | 3,532 (3,439–3,544) | -0% (noise) |
| outage_hang | call_p50_ms | 3.46 (3.4–3.5) | 3.44 (3.35–3.46) | -1% (noise) |
| outage_hang | call_p99_ms | 5.72 (5.53–5.75) | 5.64 (5.44–5.72) | -1% (noise) |
| outage_hang | added_call_p50_ms | 1.35 (1.28–1.37) | 1.28 (1.23–1.31) | -5% (noise) |
| outage_hang | added_call_p99_ms | -0.434 (-0.798–-0.299) | -0.613 (-2.99–-0.28) | -41% (noise) |
| outage_hang | close_ms | 1,119 (1,075–1,179) | 1,126 (1,116–1,221) | +1% (noise) |
| outage_hang | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | plain_wall_ms | 2,312 (2,289–2,335) | 2,331 (2,292–2,346) | +1% (noise) |
| outage_down | plain_call_p50_ms | 2.15 (2.12–2.17) | 2.15 (2.13–2.16) | +0% (noise) |
| outage_down | plain_call_p99_ms | 6.16 (6.09–6.25) | 6.07 (5.95–6.26) | -1% |
| outage_down | wall_ms | 3,731 (3,651–3,807) | 3,734 (3,644–3,993) | +0% (noise) |
| outage_down | call_p50_ms | 3.71 (3.64–3.76) | 3.71 (3.64–3.72) | +0% (noise) |
| outage_down | call_p99_ms | 5.67 (5.54–6.03) | 5.77 (5.58–12) | +2% (noise) |
| outage_down | added_call_p50_ms | 1.58 (1.52–1.59) | 1.56 (1.51–1.58) | -2% (noise) |
| outage_down | added_call_p99_ms | -0.527 (-0.586–-0.217) | -0.365 (-0.481–5.87) | +31% (noise) |
| outage_down | close_ms | 702 (702–703) | 702 (702–703) | -0% |
| outage_down | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| memory | heap_after_warmup_bytes | 609,832 (603,436–611,160) | 609,452 (533,221–618,039) | -0% (noise) |
| memory | heap_at_half_bytes | 633,735 (617,737–638,442) | 628,688 (625,897–634,383) | -1% (noise) |
| memory | heap_at_end_bytes | 637,709 (630,005–641,084) | 636,462 (628,811–641,728) | -0% (noise) |
| memory | growth_per_call_bytes | 0.259 (0.0382–1.23) | 0.245 (0.0621–1.06) | -0.0133 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,505,225 (1,503,901–1,507,638) | 1,506,452 (1,501,479–1,508,752) | +0% (noise) |
| memory_outage | heap_at_half_bytes | 3,624,826 (3,623,849–3,635,972) | 3,635,636 (3,623,277–3,638,549) | +0% (noise) |
| memory_outage | heap_at_end_bytes | 3,637,579 (3,636,413–3,639,347) | 3,638,949 (3,637,598–3,639,403) | +0% (noise) |
| memory_outage | growth_per_call_bytes | 1.25 (0.0441–1.55) | 0.196 (0.0854–1.59) | -1.05 (noise) |
| large_result | plain_p50_ms | 0.187 (0.184–0.194) | 0.186 (0.182–0.189) | -1% (noise) |
| large_result | plain_p99_ms | 0.218 (0.207–0.252) | 0.215 (0.206–0.261) | -1% (noise) |
| large_result | baton_p50_ms | 0.427 (0.422–0.493) | 0.443 (0.44–0.5) | +4% (noise) |
| large_result | baton_p99_ms | 1.52 (1.47–1.55) | 1.54 (1.53–1.55) | +1% (noise) |
| large_result | added_p50_ms | 0.24 (0.228–0.308) | 0.255 (0.254–0.315) | +6% (noise) |
| large_result | added_p99_ms | 1.32 (1.22–1.33) | 1.33 (1.28–1.34) | +1% |
| large_result | heap_growth_bytes | 2,158,255 (2,150,988–2,159,275) | 2,158,356 (2,151,124–2,160,162) | +0% (noise) |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| serve_overhead | plain_p50_ms | 0.00019 (0.00019–0.00019) | 0.00019 (0.00019–0.0002) | +0% (noise) |
| serve_overhead | plain_p99_ms | 0.00032 (0.0003–0.000341) | 0.000321 (0.00025–0.000331) | +0% (noise) |
| serve_overhead | baton_p50_ms | 0.212 (0.209–0.212) | 0.213 (0.21–0.218) | +1% |
| serve_overhead | baton_p99_ms | 0.542 (0.535–0.549) | 0.537 (0.532–0.549) | -1% (noise) |
| serve_overhead | added_p50_ms | 0.211 (0.208–0.212) | 0.213 (0.21–0.218) | +1% |
| serve_overhead | added_p99_ms | 0.542 (0.534–0.549) | 0.537 (0.532–0.548) | -1% (noise) |
| process | max_rss_bytes | 90,484,736 (90,402,816–94,490,624) | 90,624,000 (90,431,488–95,318,016) | +0% (noise) |
