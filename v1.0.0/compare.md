## python: 0.9.7 published vs 1.0.0 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. Median (lowest–highest) of 5, 5 runs.

| measure | metric | 0.9.7 published | 1.0.0 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.528 (0.523–0.533) | 0.527 (0.515–0.55) | -0% (noise) |
| overhead | plain_p99_ms | 0.616 (0.592–0.633) | 0.593 (0.57–0.609) | -4% (noise) |
| overhead | baton_p50_ms | 0.974 (0.959–1) | 1 (0.979–1.08) | +3% |
| overhead | baton_p99_ms | 1.59 (1.53–1.63) | 1.6 (1.54–1.7) | +1% (noise) |
| overhead | added_p50_ms | 0.446 (0.436–0.468) | 0.475 (0.459–0.539) | +7% |
| overhead | added_p99_ms | 0.962 (0.914–1.03) | 1.01 (0.946–1.12) | +5% (noise) |
| burst | wall_ms | 9,626 (9,594–9,723) | 9,476 (9,461–9,871) | -2% |
| burst | call_p50_ms | 9.43 (9.39–9.54) | 9.28 (9.25–9.64) | -2% |
| burst | call_p99_ms | 13.7 (13.5–13.9) | 13.5 (13.3–14.1) | -1% |
| burst | close_ms | 1.56 (1.38–2.15) | 1.67 (1.36–2.06) | +7% (noise) |
| burst | emitted | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | delivered | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | wall_ms | 1,617 (1,615–1,638) | 1,609 (1,579–1,738) | -0% |
| outage_hang | call_p50_ms | 7.92 (7.88–8.05) | 7.87 (7.69–8.48) | -1% |
| outage_hang | call_p99_ms | 12.2 (11.9–12.5) | 11.9 (11.6–13) | -3% |
| outage_hang | close_ms | 3,005 (3,005–3,007) | 3,005 (3,005–3,005) | +0% (noise) |
| outage_hang | emitted | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_down | wall_ms | 1,684 (1,680–1,686) | 1,690 (1,659–1,712) | +0% |
| outage_down | call_p50_ms | 8.24 (8.22–8.29) | 8.26 (8.11–8.36) | +0% (noise) |
| outage_down | call_p99_ms | 12.1 (12.1–12.3) | 12.3 (11.9–12.8) | +1% (noise) |
| outage_down | close_ms | 706 (706–706) | 706 (706–706) | +0% (noise) |
| outage_down | emitted | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| memory | heap_after_warmup_bytes | 609,304 (603,721–611,956) | 616,478 (605,573–619,921) | +1% |
| memory | heap_at_half_bytes | 640,477 (633,136–647,649) | 629,788 (627,987–640,309) | -2% |
| memory | heap_at_end_bytes | 640,458 (623,431–642,930) | 630,225 (628,780–631,973) | -2% (noise) |
| memory | growth_per_call_bytes | -0.706 (-2.09–0.979) | 0.0558 (-0.856–0.0793) | +0.762 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,503,272 (1,499,437–1,504,647) | 1,501,399 (1,497,647–1,504,402) | -0% (noise) |
| memory_outage | heap_at_half_bytes | 3,632,499 (3,631,174–3,639,042) | 3,631,197 (3,630,375–3,636,336) | -0% (noise) |
| memory_outage | heap_at_end_bytes | 3,542,328 (3,541,335–3,548,656) | 3,541,487 (3,540,321–3,546,311) | -0% (noise) |
| memory_outage | growth_per_call_bytes | -9.02 (-9.11–-8.82) | -9 (-9.05–-8.97) | +0% (noise) |
| large_result | plain_p50_ms | 0.469 (0.451–0.496) | 0.466 (0.462–0.486) | -1% (noise) |
| large_result | plain_p99_ms | 0.547 (0.532–0.559) | 0.53 (0.514–0.68) | -3% |
| large_result | baton_p50_ms | 1 (1–1.14) | 1.01 (1–1.02) | +1% (noise) |
| large_result | baton_p99_ms | 2.73 (2.66–2.82) | 2.84 (2.8–2.93) | +4% |
| large_result | added_p50_ms | 0.553 (0.533–0.641) | 0.543 (0.534–0.554) | -2% (noise) |
| large_result | added_p99_ms | 2.18 (2.11–2.27) | 2.29 (2.23–2.41) | +5% |
| large_result | heap_growth_bytes | 2,151,921 (2,149,723–2,159,427) | 2,150,424 (2,149,527–2,159,186) | -0% (noise) |
| large_result | emitted | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | delivered | 2,200 (2,200–2,200) | 2,200 (2,200–2,200) | same |
| large_result | lost | 0 (0–0) | 0 (0–0) | same |
| process | max_rss_bytes | 86,917,120 (86,802,432–92,381,184) | 86,941,696 (86,695,936–91,262,976) | +0% (noise) |
