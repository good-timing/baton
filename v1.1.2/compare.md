## python: 1.1.1 published vs 1.1.2 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. The exception is a measure's 'limits': over 'repeats' runs of that measure alone, each named metric's median must be under its limit. scripts/check_bench_limit.py runs and checks them. A measure's 'noise_floor' gives, per metric and SDK, the change between runs of one version that is still noise. Median (lowest–highest) of 5, 5 runs.

CPU: 1.1.1 published: AMD EPYC 9V45 96-Core Processor (5); 1.1.2 checkout: AMD EPYC 9V45 96-Core Processor (5)

| measure | metric | 1.1.1 published | 1.1.2 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.228 (0.226–0.231) | 0.222 (0.214–0.23) | -3% |
| overhead | plain_p99_ms | 0.318 (0.264–0.414) | 0.264 (0.245–0.266) | -17% (noise) |
| overhead | baton_p50_ms | 0.45 (0.418–0.521) | 0.44 (0.415–0.496) | -2% (noise) |
| overhead | baton_p99_ms | 0.778 (0.773–1.48) | 0.821 (0.741–1.37) | +6% (noise) |
| overhead | added_p50_ms | 0.222 (0.187–0.291) | 0.21 (0.195–0.282) | -5% (noise) |
| overhead | added_p99_ms | 0.509 (0.363–1.13) | 0.557 (0.477–1.11) | +9% (noise) |
| burst | plain_wall_ms | 2,568 (2,514–2,614) | 2,506 (2,449–2,535) | -2% |
| burst | plain_call_p50_ms | 2.29 (2.24–2.33) | 2.28 (2.23–2.3) | -1% (noise) |
| burst | plain_call_p99_ms | 6.45 (6.26–7.89) | 6.51 (6.48–6.95) | +1% (noise) |
| burst | wall_ms | 4,578 (4,439–4,791) | 4,589 (4,402–4,719) | +0% (noise) |
| burst | call_p50_ms | 4.39 (4.25–4.58) | 4.39 (4.22–4.43) | -0% (noise) |
| burst | call_p99_ms | 7.5 (7.26–9.65) | 7.84 (7.4–13.1) | +4% (noise) |
| burst | added_call_p50_ms | 2.1 (2–2.25) | 2.1 (1.99–2.15) | +0% (noise) |
| burst | added_call_p99_ms | 1.09 (-0.523–3.2) | 1.1 (0.885–6.58) | +0.00586 (noise) |
| burst | close_ms | 1.53 (1.07–2.45) | 1.81 (1.57–2.13) | +18% (noise) |
| burst | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | delivered | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | plain_wall_ms | 2,554 (2,439–2,567) | 2,523 (2,484–2,601) | -1% (noise) |
| outage_hang | plain_call_p50_ms | 2.28 (2.21–2.3) | 2.26 (2.24–2.31) | -1% (noise) |
| outage_hang | plain_call_p99_ms | 6.54 (6.36–7.29) | 6.57 (6.16–7.09) | +0% (noise) |
| outage_hang | wall_ms | 3,720 (3,677–3,778) | 3,734 (3,618–3,796) | +0% (noise) |
| outage_hang | call_p50_ms | 3.61 (3.58–3.66) | 3.61 (3.52–3.65) | +0% (noise) |
| outage_hang | call_p99_ms | 6.28 (5.87–6.49) | 6.07 (5.89–7.97) | -3% (noise) |
| outage_hang | added_call_p50_ms | 1.35 (1.31–1.38) | 1.3 (1.28–1.37) | -4% |
| outage_hang | added_call_p99_ms | -0.506 (-1–-0.0319) | -0.277 (-0.597–0.885) | +45% (noise) |
| outage_hang | close_ms | 939 (883–980) | 929 (864–1,043) | -1% (noise) |
| outage_hang | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | plain_wall_ms | 2,525 (2,423–2,543) | 2,498 (2,421–2,546) | -1% (noise) |
| outage_down | plain_call_p50_ms | 2.26 (2.21–2.28) | 2.25 (2.17–2.27) | -1% (noise) |
| outage_down | plain_call_p99_ms | 6.48 (6.2–6.85) | 6.73 (6.29–6.9) | +4% (noise) |
| outage_down | wall_ms | 3,976 (3,906–4,124) | 3,955 (3,776–4,064) | -1% (noise) |
| outage_down | call_p50_ms | 3.91 (3.8–3.95) | 3.89 (3.67–3.9) | -1% (noise) |
| outage_down | call_p99_ms | 6.9 (6.59–9.26) | 7.06 (6.44–8.32) | +2% (noise) |
| outage_down | added_call_p50_ms | 1.63 (1.58–1.72) | 1.62 (1.49–1.67) | -0% (noise) |
| outage_down | added_call_p99_ms | 0.421 (0.108–2.4) | 0.774 (-0.42–1.59) | +84% (noise) |
| outage_down | close_ms | 705 (705–706) | 705 (705–705) | -0% (noise) |
| outage_down | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| memory | heap_after_warmup_bytes | 606,525 (604,661–615,287) | 608,154 (528,170–633,236) | +0% (noise) |
| memory | heap_at_half_bytes | 627,157 (622,145–640,503) | 632,423 (628,257–651,093) | +1% (noise) |
| memory | heap_at_end_bytes | 631,599 (622,401–636,921) | 639,230 (626,524–642,400) | +1% |
| memory | growth_per_call_bytes | 0.108 (-0.89–0.976) | 0.0375 (-1.11–0.998) | -0.0707 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,507,553 (1,506,258–1,508,187) | 1,506,211 (1,504,537–1,506,882) | -0% |
| memory_outage | heap_at_half_bytes | 3,637,894 (3,635,540–3,639,429) | 3,637,418 (3,636,913–3,638,411) | -0% (noise) |
| memory_outage | heap_at_end_bytes | 3,548,127 (3,547,009–3,639,986) | 3,548,028 (3,546,649–3,549,111) | -0% (noise) |
| memory_outage | growth_per_call_bytes | -8.89 (-9.13–0.181) | -8.92 (-9.07–-8.9) | -0.0355 (noise) |
| large_result | plain_p50_ms | 0.196 (0.191–0.206) | 0.199 (0.193–0.208) | +1% (noise) |
| large_result | plain_p99_ms | 0.226 (0.223–0.461) | 0.242 (0.215–0.58) | +7% (noise) |
| large_result | baton_p50_ms | 0.471 (0.462–0.501) | 0.477 (0.454–0.518) | +1% (noise) |
| large_result | baton_p99_ms | 1.67 (1.6–1.85) | 1.72 (1.62–1.91) | +3% (noise) |
| large_result | added_p50_ms | 0.268 (0.267–0.31) | 0.278 (0.248–0.319) | +4% (noise) |
| large_result | added_p99_ms | 1.43 (1.15–1.63) | 1.4 (1.13–1.68) | -3% (noise) |
| large_result | heap_growth_bytes | 2,153,340 (2,149,219–2,160,134) | 2,150,474 (2,148,880–2,151,859) | -0% (noise) |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| process | max_rss_bytes | 90,345,472 (90,038,272–94,670,848) | 90,210,304 (89,571,328–94,633,984) | -0% (noise) |
