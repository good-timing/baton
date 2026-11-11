## python: 1.1.1 published vs 1.1.2 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. The exception is a measure's 'limits': over 'repeats' runs of that measure alone, each named metric's median must be under its limit. scripts/check_bench_limit.py runs and checks them. A measure's 'noise_floor' gives, per metric and SDK, the change between runs of one version that is still noise. Median (lowest–highest) of 5, 5 runs.

CPU: 1.1.1 published: AMD EPYC 9V74 80-Core Processor (5); 1.1.2 checkout: AMD EPYC 9V74 80-Core Processor (5)

| measure | metric | 1.1.1 published | 1.1.2 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.331 (0.324–0.352) | 0.344 (0.325–0.354) | +4% (noise) |
| overhead | plain_p99_ms | 0.389 (0.372–0.398) | 0.399 (0.38–0.555) | +3% |
| overhead | baton_p50_ms | 0.641 (0.63–0.648) | 0.643 (0.634–0.756) | +0% (noise) |
| overhead | baton_p99_ms | 1.17 (1.11–1.52) | 1.15 (1.13–1.22) | -1% (noise) |
| overhead | added_p50_ms | 0.303 (0.288–0.324) | 0.309 (0.29–0.405) | +2% (noise) |
| overhead | added_p99_ms | 0.771 (0.729–1.12) | 0.75 (0.657–0.825) | -3% (noise) |
| burst | plain_wall_ms | 3,568 (3,549–3,596) | 3,572 (3,527–3,589) | +0% (noise) |
| burst | plain_call_p50_ms | 3.32 (3.28–3.36) | 3.3 (3.28–3.32) | -1% (noise) |
| burst | plain_call_p99_ms | 9 (8.8–9.38) | 8.93 (8.66–9.3) | -1% (noise) |
| burst | wall_ms | 6,930 (6,862–6,953) | 6,885 (6,856–6,953) | -1% (noise) |
| burst | call_p50_ms | 6.88 (6.81–6.92) | 6.83 (6.82–6.91) | -1% (noise) |
| burst | call_p99_ms | 9.42 (8.93–9.55) | 9.08 (9.03–9.25) | -4% (noise) |
| burst | added_call_p50_ms | 3.56 (3.53–3.58) | 3.55 (3.5–3.61) | -0% (noise) |
| burst | added_call_p99_ms | 0.169 (-0.00536–0.592) | 0.202 (-0.212–0.369) | +0.0328 (noise) |
| burst | close_ms | 2 (1.39–2.07) | 2.01 (1.98–2.32) | +0% (noise) |
| burst | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | delivered | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | plain_wall_ms | 3,583 (3,560–3,602) | 3,567 (3,531–3,569) | -0% (noise) |
| outage_hang | plain_call_p50_ms | 3.32 (3.29–3.34) | 3.31 (3.29–3.33) | -0% (noise) |
| outage_hang | plain_call_p99_ms | 9.06 (8.79–9.14) | 8.9 (8.71–8.95) | -2% (noise) |
| outage_hang | wall_ms | 5,600 (5,546–5,648) | 5,592 (5,546–5,668) | -0% (noise) |
| outage_hang | call_p50_ms | 5.55 (5.45–5.58) | 5.53 (5.48–5.6) | -0% (noise) |
| outage_hang | call_p99_ms | 7.36 (7.25–7.61) | 7.31 (7.13–7.81) | -1% (noise) |
| outage_hang | added_call_p50_ms | 2.22 (2.16–2.25) | 2.23 (2.18–2.3) | +0% (noise) |
| outage_hang | added_call_p99_ms | -1.6 (-1.83–-1.24) | -1.54 (-1.6–-1.13) | +4% (noise) |
| outage_hang | close_ms | 3,005 (3,005–3,005) | 3,005 (3,005–3,005) | +0% |
| outage_hang | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | plain_wall_ms | 3,581 (3,541–3,594) | 3,560 (3,541–3,597) | -1% (noise) |
| outage_down | plain_call_p50_ms | 3.32 (3.27–3.33) | 3.31 (3.28–3.34) | -1% (noise) |
| outage_down | plain_call_p99_ms | 9.07 (8.81–9.21) | 8.85 (8.46–9.12) | -2% (noise) |
| outage_down | wall_ms | 6,014 (5,967–6,040) | 6,005 (5,987–6,044) | -0% (noise) |
| outage_down | call_p50_ms | 5.98 (5.94–6.03) | 5.96 (5.96–6.04) | -0% (noise) |
| outage_down | call_p99_ms | 7.91 (7.72–8.01) | 7.9 (7.77–8.52) | -0% (noise) |
| outage_down | added_call_p50_ms | 2.68 (2.63–2.7) | 2.68 (2.63–2.69) | -0% (noise) |
| outage_down | added_call_p99_ms | -1.13 (-1.42–-0.878) | -0.693 (-1.28–-0.325) | +39% |
| outage_down | close_ms | 336 (304–380) | 338 (294–356) | +1% (noise) |
| outage_down | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| memory | heap_after_warmup_bytes | 609,164 (526,840–616,654) | 615,238 (605,998–622,438) | +1% (noise) |
| memory | heap_at_half_bytes | 628,725 (618,204–644,849) | 631,348 (619,636–635,084) | +0% (noise) |
| memory | heap_at_end_bytes | 628,794 (619,327–642,235) | 632,359 (548,851–643,853) | +1% (noise) |
| memory | growth_per_call_bytes | 0.112 (-0.366–0.495) | 0.078 (-8.62–1.25) | -0.0343 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,505,222 (1,503,881–1,507,150) | 1,504,427 (1,500,553–1,506,012) | -0% (noise) |
| memory_outage | heap_at_half_bytes | 3,547,072 (3,545,736–3,547,885) | 3,547,318 (3,546,652–3,548,083) | +0% (noise) |
| memory_outage | heap_at_end_bytes | 3,638,961 (3,637,337–3,640,086) | 3,639,282 (3,548,583–3,639,774) | +0% (noise) |
| memory_outage | growth_per_call_bytes | 9.19 (9.09–9.28) | 9.16 (0.0888–9.31) | -0.0273 (noise) |
| large_result | plain_p50_ms | 0.298 (0.279–0.307) | 0.292 (0.277–0.304) | -2% (noise) |
| large_result | plain_p99_ms | 0.343 (0.315–0.349) | 0.344 (0.323–0.356) | +0% (noise) |
| large_result | baton_p50_ms | 0.683 (0.675–0.69) | 0.684 (0.679–0.713) | +0% (noise) |
| large_result | baton_p99_ms | 2.25 (2.17–2.3) | 2.27 (2.18–2.39) | +1% (noise) |
| large_result | added_p50_ms | 0.378 (0.376–0.411) | 0.393 (0.375–0.436) | +4% (noise) |
| large_result | added_p99_ms | 1.9 (1.83–1.98) | 1.92 (1.83–2.06) | +1% (noise) |
| large_result | heap_growth_bytes | 2,152,048 (2,144,872–2,155,451) | 2,151,305 (2,148,830–2,159,950) | -0% (noise) |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| process | max_rss_bytes | 89,935,872 (89,137,152–94,003,200) | 89,165,824 (89,116,672–93,147,136) | -1% (noise) |
