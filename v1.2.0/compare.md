## python: 1.1.2 published vs 1.2.0 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. The exception is a measure's 'limits': over 'repeats' runs of that measure alone, each named metric's median must be under its limit. scripts/check_bench_limit.py runs and checks them. A measure's 'noise_floor' gives, per metric and SDK, the change between runs of one version that is still noise. Median (lowest–highest) of 5, 5 runs.

CPU: 1.1.2 published: AMD EPYC 9V74 80-Core Processor (5); 1.2.0 checkout: AMD EPYC 9V74 80-Core Processor (5)

| measure | metric | 1.1.2 published | 1.2.0 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.329 (0.322–0.353) | 0.331 (0.325–0.338) | +1% (noise) |
| overhead | plain_p99_ms | 0.39 (0.372–0.787) | 0.388 (0.369–0.399) | -1% (noise) |
| overhead | baton_p50_ms | 0.663 (0.623–0.744) | 0.638 (0.622–0.754) | -4% (noise) |
| overhead | baton_p99_ms | 1.23 (1.18–1.31) | 1.17 (1.1–1.27) | -5% |
| overhead | added_p50_ms | 0.342 (0.284–0.416) | 0.306 (0.296–0.415) | -10% (noise) |
| overhead | added_p99_ms | 0.839 (0.523–0.886) | 0.773 (0.73–0.886) | -8% (noise) |
| burst | plain_wall_ms | 3,600 (3,545–3,668) | 3,561 (3,540–3,593) | -1% (noise) |
| burst | plain_call_p50_ms | 3.32 (3.28–3.38) | 3.31 (3.27–3.32) | -0% (noise) |
| burst | plain_call_p99_ms | 9.22 (8.83–9.39) | 8.63 (8.51–9.35) | -6% |
| burst | wall_ms | 6,887 (6,859–6,920) | 6,858 (6,840–6,951) | -0% |
| burst | call_p50_ms | 6.84 (6.81–6.88) | 6.82 (6.77–6.88) | -0% (noise) |
| burst | call_p99_ms | 9.17 (8.88–9.27) | 9.22 (8.91–9.65) | +1% (noise) |
| burst | added_call_p50_ms | 3.53 (3.45–3.56) | 3.55 (3.47–3.56) | +1% (noise) |
| burst | added_call_p99_ms | 0.0146 (-0.301–0.256) | 0.427 (-0.401–1.05) | +0.412 |
| burst | close_ms | 2 (1.37–2.07) | 2.13 (1.85–2.46) | +6% |
| burst | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | delivered | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | plain_wall_ms | 3,597 (3,552–3,601) | 3,575 (3,556–3,601) | -1% (noise) |
| outage_hang | plain_call_p50_ms | 3.3 (3.28–3.32) | 3.29 (3.28–3.33) | -1% (noise) |
| outage_hang | plain_call_p99_ms | 8.98 (8.71–9.56) | 8.87 (8.76–9.36) | -1% (noise) |
| outage_hang | wall_ms | 5,605 (5,554–5,695) | 5,607 (5,487–5,638) | +0% (noise) |
| outage_hang | call_p50_ms | 5.55 (5.5–5.6) | 5.52 (5.42–5.59) | -1% (noise) |
| outage_hang | call_p99_ms | 7.4 (6.97–8.54) | 7.11 (6.96–7.81) | -4% (noise) |
| outage_hang | added_call_p50_ms | 2.26 (2.18–2.28) | 2.24 (2.13–2.26) | -1% (noise) |
| outage_hang | added_call_p99_ms | -1.38 (-2.03–-0.303) | -1.75 (-2.4–-1.25) | -27% (noise) |
| outage_hang | close_ms | 3,005 (3,003–3,005) | 3,005 (3,005–3,005) | +0% (noise) |
| outage_hang | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | plain_wall_ms | 3,582 (3,552–3,610) | 3,567 (3,532–3,631) | -0% (noise) |
| outage_down | plain_call_p50_ms | 3.32 (3.3–3.33) | 3.3 (3.28–3.33) | -1% (noise) |
| outage_down | plain_call_p99_ms | 8.98 (8.73–9.18) | 9.03 (8.86–9.32) | +1% (noise) |
| outage_down | wall_ms | 5,975 (5,964–6,006) | 5,945 (5,923–5,992) | -1% |
| outage_down | call_p50_ms | 5.96 (5.94–5.98) | 5.92 (5.88–5.94) | -1% |
| outage_down | call_p99_ms | 7.64 (7.58–8.01) | 7.78 (7.66–8.17) | +2% (noise) |
| outage_down | added_call_p50_ms | 2.65 (2.62–2.68) | 2.62 (2.6–2.64) | -1% (noise) |
| outage_down | added_call_p99_ms | -1.23 (-1.59–-0.969) | -1.2 (-1.25–-0.915) | +2% (noise) |
| outage_down | close_ms | 366 (341–380) | 394 (353–422) | +8% |
| outage_down | emitted | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 20,400 (20,400–20,400) | 20,400 (20,400–20,400) | same |
| memory | heap_after_warmup_bytes | 610,042 (594,978–623,032) | 615,234 (606,487–618,368) | +1% (noise) |
| memory | heap_at_half_bytes | 624,550 (622,105–636,496) | 628,955 (623,408–644,761) | +1% (noise) |
| memory | heap_at_end_bytes | 622,643 (614,840–634,669) | 632,612 (623,812–638,301) | +2% (noise) |
| memory | growth_per_call_bytes | -0.797 (-0.91–1.01) | 0.0404 (-0.922–0.935) | +0.838 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,506,470 (1,494,518–1,506,919) | 1,505,116 (1,503,508–1,507,529) | -0% (noise) |
| memory_outage | heap_at_half_bytes | 3,546,660 (3,520,524–3,547,070) | 3,547,636 (3,546,659–3,548,387) | +0% |
| memory_outage | heap_at_end_bytes | 3,638,791 (3,612,143–3,638,839) | 3,638,748 (3,547,930–3,639,897) | -0% (noise) |
| memory_outage | growth_per_call_bytes | 9.19 (9.16–9.21) | 9.13 (0.127–9.16) | -0.0646 (noise) |
| large_result | plain_p50_ms | 0.281 (0.279–0.298) | 0.303 (0.276–0.311) | +8% |
| large_result | plain_p99_ms | 0.345 (0.321–0.354) | 0.353 (0.316–0.58) | +2% (noise) |
| large_result | baton_p50_ms | 0.688 (0.684–0.72) | 0.687 (0.667–0.704) | -0% (noise) |
| large_result | baton_p99_ms | 2.26 (2.23–2.37) | 2.26 (2.16–2.37) | -0% (noise) |
| large_result | added_p50_ms | 0.406 (0.388–0.439) | 0.396 (0.364–0.414) | -3% (noise) |
| large_result | added_p99_ms | 1.92 (1.88–2.05) | 1.9 (1.78–1.99) | -1% (noise) |
| large_result | heap_growth_bytes | 2,152,188 (2,150,302–2,156,455) | 2,153,819 (2,150,019–2,157,533) | +0% (noise) |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| serve_overhead | plain_p50_ms | — | 0.000271 (0.000261–0.000271) | — |
| serve_overhead | plain_p99_ms | — | 0.00044 (0.00039–0.00045) | — |
| serve_overhead | baton_p50_ms | — | 0.332 (0.328–0.338) | — |
| serve_overhead | baton_p99_ms | — | 0.899 (0.823–0.99) | — |
| serve_overhead | added_p50_ms | — | 0.332 (0.328–0.338) | — |
| serve_overhead | added_p99_ms | — | 0.899 (0.823–0.989) | — |
| process | max_rss_bytes | 90,583,040 (90,447,872–95,129,600) | 91,389,952 (91,058,176–95,268,864) | +1% (noise) |
