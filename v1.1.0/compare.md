## python: 1.0.1 published vs 1.1.0 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. The exception is a measure's 'limits': over 'repeats' runs of that measure alone, each named metric's median must be under its limit. scripts/check_bench_limit.py runs and checks them. A measure's 'noise_floor' gives, per metric and SDK, the change between runs of one version that is still noise. Median (lowest–highest) of 5, 5 runs.

CPU: 1.0.1 published: AMD EPYC 9V45 96-Core Processor (5); 1.1.0 checkout: AMD EPYC 9V45 96-Core Processor (5)

| measure | metric | 1.0.1 published | 1.1.0 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.224 (0.217–0.23) | 0.227 (0.216–0.233) | +1% (noise) |
| overhead | plain_p99_ms | 0.264 (0.259–0.335) | 0.264 (0.244–0.354) | -0% (noise) |
| overhead | baton_p50_ms | 0.451 (0.432–0.455) | 0.44 (0.435–0.471) | -2% (noise) |
| overhead | baton_p99_ms | 0.932 (0.848–1.02) | 0.857 (0.746–1.7) | -8% (noise) |
| overhead | added_p50_ms | 0.225 (0.207–0.234) | 0.213 (0.21–0.255) | -5% (noise) |
| overhead | added_p99_ms | 0.668 (0.586–0.684) | 0.613 (0.482–1.34) | -8% (noise) |
| burst | wall_ms | 4,512 (4,487–5,135) | 4,628 (4,388–4,666) | +3% (noise) |
| burst | call_p50_ms | 4.34 (4.31–4.9) | 4.44 (4.23–4.46) | +2% (noise) |
| burst | call_p99_ms | 7.26 (7.12–8.14) | 7.88 (6.98–11.3) | +8% (noise) |
| burst | close_ms | 1.19 (1.05–2.72) | 1.45 (1.03–1.89) | +21% (noise) |
| burst | emitted | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | delivered | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | wall_ms | 3,903 (3,866–4,317) | 3,847 (3,645–3,913) | -1% |
| outage_hang | call_p50_ms | 3.7 (3.66–4.02) | 3.57 (3.5–3.7) | -3% |
| outage_hang | call_p99_ms | 7.97 (6.54–9.29) | 7.46 (6.06–9.43) | -6% (noise) |
| outage_hang | close_ms | 836 (422–872) | 891 (826–1,090) | +7% |
| outage_hang | emitted | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| outage_down | wall_ms | 3,966 (3,926–4,284) | 3,887 (3,802–4,037) | -2% |
| outage_down | call_p50_ms | 3.89 (3.82–4.15) | 3.83 (3.73–3.95) | -1% (noise) |
| outage_down | call_p99_ms | 6.74 (6.37–7.79) | 6.48 (6.33–7.28) | -4% (noise) |
| outage_down | close_ms | 705 (705–705) | 705 (704–705) | -0% (noise) |
| outage_down | emitted | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| memory | heap_after_warmup_bytes | 615,541 (602,401–617,522) | 620,538 (608,478–623,931) | +1% |
| memory | heap_at_half_bytes | 623,909 (570,936–638,317) | 631,577 (623,209–641,875) | +1% (noise) |
| memory | heap_at_end_bytes | 632,254 (626,723–641,449) | 632,328 (628,274–643,992) | +0% (noise) |
| memory | growth_per_call_bytes | 1.01 (0.0397–5.58) | 0.506 (-0.955–0.876) | -0.508 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,503,027 (1,497,472–1,504,174) | 1,505,447 (1,495,286–1,507,587) | +0% |
| memory_outage | heap_at_half_bytes | 3,636,698 (3,635,923–3,637,533) | 3,639,235 (3,628,152–3,640,161) | +0% |
| memory_outage | heap_at_end_bytes | 3,545,390 (3,544,855–3,546,169) | 3,549,567 (3,548,417–3,638,896) | +0% |
| memory_outage | growth_per_call_bytes | -9.15 (-9.17–-9.05) | -8.94 (-9.17–1.07) | +0.213 (noise) |
| large_result | plain_p50_ms | 0.198 (0.195–0.206) | 0.196 (0.192–0.219) | -1% (noise) |
| large_result | plain_p99_ms | 0.233 (0.218–0.243) | 0.238 (0.221–0.513) | +2% (noise) |
| large_result | baton_p50_ms | 0.465 (0.452–0.487) | 0.469 (0.443–0.516) | +1% (noise) |
| large_result | baton_p99_ms | 1.67 (1.65–1.74) | 1.68 (1.64–1.81) | +0% (noise) |
| large_result | added_p50_ms | 0.267 (0.247–0.289) | 0.27 (0.225–0.325) | +1% (noise) |
| large_result | added_p99_ms | 1.44 (1.41–1.51) | 1.43 (1.22–1.58) | -1% (noise) |
| large_result | heap_growth_bytes | 2,154,312 (2,144,382–2,158,532) | 2,158,919 (2,145,386–2,160,879) | +0% |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| process | max_rss_bytes | 85,504,000 (85,159,936–90,660,864) | 85,344,256 (85,131,264–89,534,464) | -0% (noise) |
