# Job Hunter Automation Documentation

## 1. How It Was Created
The Job Hunter automation was built as a unified Python script (`job_hunter.py`) aimed at finding, scoring, and matching internships based on a user's specific resume and preferences. It integrates multiple different technologies:
- **Web Scraping & APIs:** Uses `requests` and `BeautifulSoup4` for parsing static HTML (LinkedIn), and `Playwright` to intercept dynamic XHR/fetch requests in single-page applications (Handshake). It also integrates `SerpApi` to seamlessly search Google Jobs and Indeed.
- **AI Integration:** Implements `google-genai` (Gemini API) and `OpenAI` to intelligently read job descriptions and compare them against Koustubh's resume for a compatibility score.
- **Document Generation:** Uses `python-docx` to automatically generate tailored cover letters based on the matched job description and the user's resume.
- **Alerting System:** Pushes structured and formatted job alerts to a specified Discord Webhook, notifying the user immediately when a high-quality job is found.

## 2. How It Works
The system follows a continuous cycle:
1. **Source Collection:**
   - **LinkedIn:** Searches the public guest-jobs endpoint matching specific keywords.
   - **Handshake:** Uses a saved authenticated browser session (via `auth_state.json`) in Playwright to simulate a user search, intercepting the underlying GraphQL/REST API responses to extract job listings reliably.
   - **SerpApi:** Queries Google Jobs for roles on Indeed and other boards, rotating through a list of API keys in `.env` if quotas are exhausted.
2. **Deduplication:** Jobs are converted to a unique ID (from their URL) and checked against a `seen_jobs.json` file to ensure no duplicate alerts are evaluated or sent.
3. **Evaluation Loop:** Each new job's description and pay information is fully scraped. The AI (Gemini or OpenAI) evaluates the job relevance against the stored resume, returning a score out of 100 and a 1-2 sentence justification.
4. **Filtering and Alerting:** Jobs scoring above the `MATCH_THRESHOLD` (e.g., 70), or those above 60 but with explicitly high pay, trigger an alert.
5. **Cover Letter Generation:** For passing jobs, the AI creates a customized cover letter text, which is saved as a `.docx` file in the `cover_letters` directory.
6. **Notification:** The job details, score justification, and file paths are compiled into an embedded Discord message and sent via webhook.
7. **Sleep & Repeat:** The script sleeps for a configured interval (default 30 minutes) and begins again.

## 3. Current Limitations
- **Scraping Fragility:** Both LinkedIn and Handshake scraping methods are fragile. If LinkedIn updates its public job card CSS classes, the parser will break. If Handshake changes its GraphQL schema or prevents headless session reuse, that source will fail.
- **Authentication Maintenance:** The Handshake authentication state (`auth_state.json`) will eventually expire. The user must manually run the script with `--setup-auth` when this happens.
- **API Limits & Costs:** SerpApi keys have rigid monthly quotas, requiring external manual rotation or multiple keys. Gemini/OpenAI reliance means potential rate limit HTTP errors or API costs/tokens running out.
- **False Positives/Negatives:** The LLM-based scoring can hallucinate or inaccurately score jobs due to contextual misunderstandings or missing information in the job descriptions. Cover letters may occasionally contain generic or hallucinatory text if the AI prompt does not tightly constrain the output.
- **Rate Limiting:** Unauthenticated scraping, particularly on LinkedIn, faces aggressive IP rate-limiting, which the script tries to mitigate using randomized User-Agents and sleep intervals.
