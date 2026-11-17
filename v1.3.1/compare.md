## python: 1.3.0 published vs 1.3.1 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. The exception is a measure's 'limits': over 'repeats' runs of that measure alone, each named metric's median must be under its limit. scripts/check_bench_limit.py runs and checks them. A measure's 'noise_floor' gives, per metric and SDK, the change between runs of one version that is still noise. Median (lowest–highest) of 5, 5 runs.

CPU: 1.3.0 published: AMD EPYC 7763 64-Core Processor (5); 1.3.1 checkout: AMD EPYC 7763 64-Core Processor (5)

| measure | metric | 1.3.0 published | 1.3.1 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.535 (0.526–0.548) | 0.53 (0.524–0.554) | -1% (noise) |
| overhead | plain_p99_ms | 0.616 (0.601–0.634) | 0.608 (0.597–0.625) | -1% (noise) |
| overhead | baton_p50_ms | 0.989 (0.982–1.11) | 1.01 (0.983–1.09) | +2% (noise) |
| overhead | baton_p99_ms | 1.65 (1.56–1.88) | 1.67 (1.51–1.81) | +1% (noise) |
| overhead | added_p50_ms | 0.456 (0.441–0.572) | 0.481 (0.459–0.536) | +5% (noise) |
| overhead | added_p99_ms | 1.05 (0.945–1.25) | 1.08 (0.911–1.2) | +3% (noise) |
| burst | plain_wall_ms | 5,706 (5,485–5,794) | 5,715 (5,512–5,807) | +0% (noise) |
| burst | plain_call_p50_ms | 5.25 (4.98–5.3) | 5.28 (5–5.36) | +1% (noise) |
| burst | plain_call_p99_ms | 14.5 (14.3–15.5) | 14.7 (14.2–15.2) | +1% (noise) |
| burst | wall_ms | 9,896 (9,777–10,183) | 9,974 (9,747–10,319) | +1% (noise) |
| burst | call_p50_ms | 9.68 (9.59–9.97) | 9.77 (9.58–10.1) | +1% (noise) |
| burst | call_p99_ms | 13.8 (13.7–14.2) | 14.1 (13.7–14.7) | +2% (noise) |
| burst | added_call_p50_ms | 4.55 (4.39–4.67) | 4.58 (4.33–4.71) | +1% (noise) |
| burst | added_call_p99_ms | -0.719 (-1.68–-0.137) | -0.473 (-1.06–-0.219) | +34% (noise) |
| burst | close_ms | 2.92 (2.74–3.32) | 3.12 (2–3.19) | +7% (noise) |
| burst | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | delivered | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | plain_wall_ms | 5,684 (5,584–5,718) | 5,678 (5,539–5,863) | -0% (noise) |
| outage_hang | plain_call_p50_ms | 5.19 (5.1–5.24) | 5.22 (5.07–5.36) | +1% (noise) |
| outage_hang | plain_call_p99_ms | 14.7 (14.4–15) | 14.6 (14.3–15.3) | -1% (noise) |
| outage_hang | wall_ms | 8,633 (8,474–8,910) | 8,622 (8,436–8,736) | -0% (noise) |
| outage_hang | call_p50_ms | 8.49 (8.33–8.8) | 8.49 (8.32–8.59) | +0% (noise) |
| outage_hang | call_p99_ms | 12.2 (11.8–12.5) | 12.2 (11.6–12.5) | -0% (noise) |
| outage_hang | added_call_p50_ms | 3.29 (3.21–3.61) | 3.23 (3.2–3.37) | -2% (noise) |
| outage_hang | added_call_p99_ms | -2.51 (-3.01–-2.07) | -2.75 (-3.02–-2.05) | -9% (noise) |
| outage_hang | close_ms | 3,005 (3,005–3,005) | 3,005 (3,005–3,005) | -0% (noise) |
| outage_hang | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | plain_wall_ms | 5,610 (5,546–5,900) | 5,748 (5,528–5,927) | +2% (noise) |
| outage_down | plain_call_p50_ms | 5.14 (5.05–5.43) | 5.23 (5.06–5.38) | +2% (noise) |
| outage_down | plain_call_p99_ms | 15.2 (14.6–15.4) | 14.9 (14.4–15.4) | -1% (noise) |
| outage_down | wall_ms | 8,984 (8,773–9,117) | 8,926 (8,706–9,089) | -1% (noise) |
| outage_down | call_p50_ms | 8.87 (8.67–9.05) | 8.81 (8.61–8.96) | -1% (noise) |
| outage_down | call_p99_ms | 12.5 (12.3–13) | 12.5 (12.2–12.8) | +0% (noise) |
| outage_down | added_call_p50_ms | 3.62 (3.53–3.75) | 3.59 (3.43–3.63) | -1% (noise) |
| outage_down | added_call_p99_ms | -2.7 (-2.8–-2.17) | -2.3 (-2.87–-2.13) | +15% (noise) |
| outage_down | close_ms | 706 (706–707) | 706 (706–707) | -0% (noise) |
| outage_down | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| memory | heap_after_warmup_bytes | 605,273 (603,379–616,393) | 609,259 (604,003–620,091) | +1% (noise) |
| memory | heap_at_half_bytes | 634,267 (619,755–639,303) | 635,259 (624,091–640,618) | +0% (noise) |
| memory | heap_at_end_bytes | 635,034 (630,051–636,031) | 631,457 (625,247–640,081) | -1% (noise) |
| memory | growth_per_call_bytes | 0.112 (-0.895–1.51) | 0.0906 (-0.916–0.915) | -0.0209 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,505,253 (1,499,593–1,506,610) | 1,505,226 (1,503,062–1,505,557) | -0% (noise) |
| memory_outage | heap_at_half_bytes | 3,618,450 (3,539,887–3,631,325) | 3,630,088 (3,625,906–3,631,137) | +0% (noise) |
| memory_outage | heap_at_end_bytes | 3,541,065 (3,540,846–3,542,140) | 3,541,229 (3,540,228–3,542,148) | +0% (noise) |
| memory_outage | growth_per_call_bytes | -7.72 (-9.02–0.118) | -8.89 (-9.09–-8.38) | -1.17 (noise) |
| large_result | plain_p50_ms | 0.486 (0.456–0.502) | 0.483 (0.46–0.503) | -1% (noise) |
| large_result | plain_p99_ms | 0.556 (0.538–0.645) | 0.531 (0.51–0.567) | -5% |
| large_result | baton_p50_ms | 1.05 (1.03–1.18) | 1.04 (1.01–1.14) | -1% (noise) |
| large_result | baton_p99_ms | 3 (2.67–3.58) | 2.95 (2.75–3.25) | -1% (noise) |
| large_result | added_p50_ms | 0.58 (0.525–0.689) | 0.55 (0.54–0.659) | -5% (noise) |
| large_result | added_p99_ms | 2.43 (2.11–3.04) | 2.39 (2.23–2.72) | -2% (noise) |
| large_result | heap_growth_bytes | 2,159,224 (2,147,378–2,160,123) | 2,154,201 (2,151,809–2,158,845) | -0% (noise) |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| serve_overhead | plain_p50_ms | 0.000371 (0.000371–0.000381) | 0.000381 (0.000371–0.00041) | +3% (noise) |
| serve_overhead | plain_p99_ms | 0.000511 (0.000491–0.000621) | 0.000561 (0.000511–0.000611) | +10% (noise) |
| serve_overhead | baton_p50_ms | 0.564 (0.545–0.581) | 0.556 (0.539–0.558) | -1% (noise) |
| serve_overhead | baton_p99_ms | 1.32 (1.11–1.33) | 1.28 (1.19–1.36) | -3% (noise) |
| serve_overhead | added_p50_ms | 0.563 (0.544–0.58) | 0.555 (0.539–0.558) | -1% (noise) |
| serve_overhead | added_p99_ms | 1.32 (1.11–1.33) | 1.28 (1.19–1.36) | -3% (noise) |
| process | max_rss_bytes | 91,021,312 (90,972,160–95,694,848) | 90,959,872 (90,378,240–95,780,864) | -0% |
