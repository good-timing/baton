## python: 1.3.2 published vs 1.3.3 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. The exception is a measure's 'limits': over 'repeats' runs of that measure alone, each named metric's median must be under its limit. scripts/check_bench_limit.py runs and checks them. A measure's 'noise_floor' gives, per metric and SDK, the change between runs of one version that is still noise. Median (lowest–highest) of 5, 5 runs.

CPU: 1.3.2 published: Intel(R) Xeon(R) Platinum 8370C CPU @ 2.80GHz (5); 1.3.3 checkout: Intel(R) Xeon(R) Platinum 8370C CPU @ 2.80GHz (5)

| measure | metric | 1.3.2 published | 1.3.3 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.493 (0.487–0.504) | 0.509 (0.488–0.522) | +3% |
| overhead | plain_p99_ms | 0.567 (0.563–0.593) | 0.573 (0.562–0.976) | +1% (noise) |
| overhead | baton_p50_ms | 0.995 (0.877–1.03) | 0.885 (0.87–0.924) | -11% (noise) |
| overhead | baton_p99_ms | 1.57 (1.47–1.61) | 1.53 (1.52–1.56) | -3% (noise) |
| overhead | added_p50_ms | 0.502 (0.372–0.544) | 0.371 (0.361–0.402) | -26% |
| overhead | added_p99_ms | 0.996 (0.875–1.05) | 0.951 (0.551–0.975) | -5% (noise) |
| burst | plain_wall_ms | 4,776 (4,733–4,996) | 4,816 (4,768–4,908) | +1% (noise) |
| burst | plain_call_p50_ms | 4.26 (4.21–4.48) | 4.32 (4.22–4.36) | +2% (noise) |
| burst | plain_call_p99_ms | 13.5 (13.2–13.9) | 13.6 (13–14) | +1% (noise) |
| burst | wall_ms | 8,947 (8,805–9,275) | 8,982 (8,851–9,193) | +0% (noise) |
| burst | call_p50_ms | 8.77 (8.59–9.04) | 8.81 (8.64–9.04) | +0% (noise) |
| burst | call_p99_ms | 14 (13.9–14.5) | 13.9 (13.9–14.6) | -1% (noise) |
| burst | added_call_p50_ms | 4.49 (4.33–4.57) | 4.52 (4.43–4.68) | +0% (noise) |
| burst | added_call_p99_ms | 0.596 (0.45–0.88) | 0.51 (-0.0947–1.59) | -14% (noise) |
| burst | close_ms | 2.64 (2.55–2.95) | 2.72 (1.75–2.91) | +3% (noise) |
| burst | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | delivered | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | plain_wall_ms | 4,747 (4,728–4,933) | 4,887 (4,758–4,918) | +3% (noise) |
| outage_hang | plain_call_p50_ms | 4.25 (4.18–4.43) | 4.36 (4.26–4.39) | +3% (noise) |
| outage_hang | plain_call_p99_ms | 13.3 (13.1–13.5) | 13.6 (13.1–13.8) | +2% |
| outage_hang | wall_ms | 7,862 (7,802–8,020) | 7,951 (7,811–8,003) | +1% (noise) |
| outage_hang | call_p50_ms | 7.62 (7.56–7.81) | 7.7 (7.57–7.78) | +1% (noise) |
| outage_hang | call_p99_ms | 13 (12.9–13.4) | 13 (12.9–13.2) | -0% (noise) |
| outage_hang | added_call_p50_ms | 3.38 (3.28–3.55) | 3.33 (3.27–3.45) | -1% (noise) |
| outage_hang | added_call_p99_ms | -0.189 (-0.489–-0.0846) | -0.431 (-0.917–-0.186) | -128% (noise) |
| outage_hang | close_ms | 3,005 (3,005–3,005) | 3,005 (3,005–3,005) | -0% |
| outage_hang | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | plain_wall_ms | 4,827 (4,748–4,917) | 4,806 (4,763–4,943) | -0% (noise) |
| outage_down | plain_call_p50_ms | 4.34 (4.22–4.36) | 4.27 (4.26–4.43) | -2% (noise) |
| outage_down | plain_call_p99_ms | 13.4 (12.8–13.8) | 13.2 (13–13.8) | -1% (noise) |
| outage_down | wall_ms | 8,145 (8,087–8,279) | 8,111 (8,034–8,399) | -0% (noise) |
| outage_down | call_p50_ms | 7.92 (7.81–8.07) | 7.9 (7.77–8.22) | -0% (noise) |
| outage_down | call_p99_ms | 13.4 (13–13.7) | 13.2 (13.1–13.6) | -1% (noise) |
| outage_down | added_call_p50_ms | 3.62 (3.57–3.73) | 3.63 (3.51–3.79) | +0% (noise) |
| outage_down | added_call_p99_ms | -0.0119 (-0.804–0.892) | -0.0351 (-0.202–0.0417) | -0.0232 (noise) |
| outage_down | close_ms | 706 (706–707) | 706 (706–706) | -0% (noise) |
| outage_down | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| memory | heap_after_warmup_bytes | 603,529 (532,102–629,490) | 607,785 (601,625–609,874) | +1% (noise) |
| memory | heap_at_half_bytes | 620,665 (614,569–628,713) | 620,503 (619,635–646,345) | -0% (noise) |
| memory | heap_at_end_bytes | 621,920 (617,724–629,207) | 626,074 (619,823–635,173) | +1% (noise) |
| memory | growth_per_call_bytes | 0.0766 (0.0312–0.9) | 0.18 (-1.12–0.557) | +0.104 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,505,028 (1,502,677–1,506,809) | 1,504,327 (1,502,206–1,504,950) | -0% (noise) |
| memory_outage | heap_at_half_bytes | 3,618,509 (3,617,626–3,628,746) | 3,630,117 (3,617,268–3,631,321) | +0% |
| memory_outage | heap_at_end_bytes | 3,540,295 (3,537,881–3,541,662) | 3,540,973 (3,540,403–3,541,876) | +0% (noise) |
| memory_outage | growth_per_call_bytes | -7.73 (-8.89–-7.68) | -8.97 (-9.03–-7.54) | -1.24 (noise) |
| large_result | plain_p50_ms | 0.419 (0.41–0.44) | 0.417 (0.408–0.457) | -0% (noise) |
| large_result | plain_p99_ms | 0.498 (0.467–0.583) | 0.48 (0.463–0.544) | -3% (noise) |
| large_result | baton_p50_ms | 0.942 (0.921–0.974) | 0.941 (0.937–0.959) | -0% (noise) |
| large_result | baton_p99_ms | 2.67 (2.56–2.81) | 2.67 (2.64–2.75) | -0% (noise) |
| large_result | added_p50_ms | 0.511 (0.508–0.545) | 0.524 (0.492–0.533) | +3% (noise) |
| large_result | added_p99_ms | 2.18 (2.08–2.25) | 2.18 (2.13–2.26) | +0% (noise) |
| large_result | heap_growth_bytes | 2,151,471 (2,149,106–2,157,677) | 2,153,620 (2,146,888–2,158,416) | +0% (noise) |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| serve_overhead | plain_p50_ms | 0.000344 (0.000336–0.000351) | 0.000347 (0.000346–0.000514) | +1% (noise) |
| serve_overhead | plain_p99_ms | 0.00052 (0.000512–0.00058) | 0.000587 (0.000459–0.000677) | +13% |
| serve_overhead | baton_p50_ms | 0.458 (0.449–0.462) | 0.454 (0.44–0.464) | -1% (noise) |
| serve_overhead | baton_p99_ms | 1.13 (1.12–1.16) | 1.09 (1.04–1.14) | -3% |
| serve_overhead | added_p50_ms | 0.458 (0.448–0.462) | 0.453 (0.44–0.464) | -1% (noise) |
| serve_overhead | added_p99_ms | 1.13 (1.11–1.16) | 1.09 (1.04–1.14) | -3% |
| process | max_rss_bytes | 91,054,080 (90,935,296–95,817,728) | 91,123,712 (90,927,104–95,047,680) | +0% (noise) |
