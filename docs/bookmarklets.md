# Browser Bookmarklets — One-Click Job Capture

These bookmarklets add any job directly to the tracker while browsing
LinkedIn, Wellfound, Welcome to the Jungle, or HiringCafe. No extension required.

> **How it avoids browser mixed-content blocking:** instead of `fetch()` (which Chrome
> blocks on HTTPS→HTTP even for localhost), the bookmarklet uses `window.open()` to
> navigate a new tab to `http://localhost:8000/capture/add?...`. The server answers that
> GET with a confirmation page showing what it captured, and one click adds it. The GET
> itself changes nothing (spec 035): it used to add the row, which meant any page could
> do the same with an image tag. The server handles the GET,
> adds the job, and shows a result page. Plain navigation is never blocked.

---

## Prerequisites

The harrier API must be running locally before you click a bookmarklet
(`just dev` starts it on port 8000).

Verify it is alive:
```bash
curl http://localhost:8000/health
# shows the harrier health JSON with the tracker row count
```

---

## How to install a bookmarklet

1. Show the bookmarks toolbar in your browser (Cmd+Shift+B in Chrome/Firefox).
2. Right-click the toolbar → **Add page** (Chrome) or **New bookmark** (Firefox).
3. Set **Name** to something short (e.g. `+ Job`).
4. Paste the entire `javascript:…` block below into the **URL / Address** field.
5. Click **Save**.

Click the bookmark while viewing any job posting page.

---

## Universal bookmarklet (LinkedIn: Wellfound: WTTJ: HiringCafe: any page)

This single bookmarklet detects the site automatically and falls back to `<h1>` + page
title for unknown sites. It opens a small result tab — close it after you see ✅.

```javascript
javascript:(function(){var PORT=8000;var url=location.href;var src='manual';var t='',c='',loc='',desc='';function qs(s){return((document.querySelector(s)||{}).innerText||'').trim();}function svgChip(alt){var el=document.querySelector('svg[alt="'+alt+'"]');return el?(el.parentElement.querySelector('span')||{innerText:''}).innerText.trim():'';}function fromPageTitle(){var raw=(document.title.split('|')[0]||'').trim();var di=raw.lastIndexOf(' - ');if(di>0)return{t:raw.substring(0,di).trim(),c:raw.substring(di+3).trim()};return{t:raw,c:''};}function cleanTitle(title,company){var hi=title.lastIndexOf(' - http');if(hi>0)title=title.substring(0,hi).trim();if(company){var ci=title.lastIndexOf(' - ');if(ci>0&&title.substring(ci+3).trim().toLowerCase()===company.toLowerCase())title=title.substring(0,ci).trim();}return title;}if(/linkedin\.com\/jobs/.test(url)){src='linkedin_manual';var liParts=document.title.split(' | ');t=(liParts[0]||'').trim();var hi=t.lastIndexOf(' - http');if(hi>0)t=t.substring(0,hi).trim();c=(liParts.length>=3&&(liParts[liParts.length-1]||'').trim()==='LinkedIn')?(liParts[1]||'').trim():'';if(!c){var liCLinks=document.querySelectorAll('a[href*="/company/"]');for(var liCi=0;liCi<liCLinks.length;liCi++){var liClt=(liCLinks[liCi].innerText||'').trim();if(liClt&&liClt.length>=2&&liClt.length<=80&&!/^(LinkedIn|Sign in|Join now|Post a job|Post jobs for free)$/i.test(liClt)){c=liClt;break;}}}if(c){var ci=t.lastIndexOf(' - ');if(ci>0&&t.substring(ci+3).trim().toLowerCase()===c.toLowerCase())t=t.substring(0,ci).trim();}url=location.origin+location.pathname;if(c){var liEsc=c.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');var liM=document.body.innerText.match(new RegExp(liEsc+'\\s*[\u00B7\u2022]\\s*([^\n\r\u00B7\u2022]+)'));if(liM)loc=liM[1].trim();}if(!loc){var liLm=document.body.innerText.match(/([A-Za-z][^\u00B7\u2022\n\r]{2,50}?)\s*[\u00B7\u2022]\s*\d+\s*(?:hour|day|week|month|year)/i);if(liLm)loc=liLm[1].trim();}desc=qs('[data-testid="expandable-text-box"]')||qs('.jobs-description__content .jobs-description-content__text')||qs('#job-details')||qs('.jobs-description');}else if(/wellfound\.com/.test(url)){src='wellfound_manual';var si=document.querySelector('[data-test="JobListingSlideIn"]');t=si?qs('[data-test="JobListingSlideIn"] h1'):qs('h1');var wfImg=si?si.querySelector('img[alt^="Avatar for "]'):null;c=wfImg?wfImg.alt.replace(/^Avatar for /,'').trim():'';if(!c&&si){var wfLinks=si.querySelectorAll('a[href*="wellfound.com/company/"]');for(var wi=0;wi<wfLinks.length;wi++){var wlt=(wfLinks[wi].innerText||'').trim();if(wlt){c=wlt;break;}}}var wfH1=si?si.querySelector('h1'):null;var wfUl=wfH1?wfH1.nextElementSibling:null;var wfUlText=(wfUl&&wfUl.tagName==='UL')?(wfUl.innerText||'').trim():'';var wfLocParts=wfUlText.split('|');loc=wfLocParts.length>1?wfLocParts.slice(1).join('|').trim():wfLocParts[0].trim();desc=si?(si.querySelector('#job-description')||{innerText:''}).innerText.trim():qs('#job-description');if(!t||!c){var wfFb=(document.title.split('|')[0]||'').split(' at ');t=t||(wfFb[0]||'').trim();c=c||(wfFb[1]||'').trim();}}else if(/welcometothejungle\.com/.test(url)){src='wttj_manual';t=qs('[data-testid="job-metadata-block"] ~ h2');c=qs('[data-testid="job-metadata-block"] a[href*="/en/companies/"] span')||qs('[data-testid="job-metadata-block"] a[href*="/fr/entreprises/"] span');var lc=svgChip('Location'),lr=svgChip('Remote');loc=[lc,lr].filter(Boolean).join(', ');desc=qs('[data-testid="job-section-description"]');if(!t||!c){var fb=fromPageTitle();t=t||fb.t;c=c||fb.c;}if(!c){var m=url.match(/\/companies\/([^/]+)\//);if(m)c=m[1].replace(/-/g,' ');}}else if(/hiring\.cafe/.test(url)){src='hiringcafe_manual';var hcD=document.querySelector('[role="dialog"][aria-modal="true"]');if(hcD){var hcH2=hcD.querySelector('h2');t=hcH2?(hcH2.innerText||'').trim():'';var hcSS=hcD.querySelectorAll('span');for(var hcI=0;hcI<hcSS.length;hcI++){var hcSt=(hcSS[hcI].innerText||'').trim();if(/^@\s/.test(hcSt)){c=hcSt.replace(/^@\s+/,'').trim();break;}}var hcSvgs=hcD.querySelectorAll('svg');for(var hcSi=0;hcSi<hcSvgs.length;hcSi++){if((hcSvgs[hcSi].innerHTML||'').indexOf('10.5c0 7.142')>=0){var hcLp=hcSvgs[hcSi].parentElement;var hcLs=hcLp?hcLp.querySelector('span'):null;if(hcLs)loc=(hcLs.innerText||'').trim();break;}}var hcVl=hcD.querySelector('a[href^="/viewjob/"]');if(hcVl)url='https://hiring.cafe'+hcVl.getAttribute('href');var hcArt=hcD.querySelector('article');desc=hcArt?(hcArt.innerText||'').trim():'';}}else{t=qs('h1')||document.title;}if(!t){var fb2=fromPageTitle();t=fb2.t;c=c||fb2.c;}t=cleanTitle(t,c);if(!confirm('Add to tracker?\n\nTitle: '+t+'\nCompany: '+c+'\nLocation: '+loc+'\nURL: '+url))return;var p=new URLSearchParams({company:c,title:t,location:loc,url:url,source:src,description:desc.slice(0,8000)});window.open('http://localhost:'+PORT+'/capture/add?'+p.toString(),'_blank','width=480,height=260');})();
```

---

## Site-specific variants (more reliable selectors)

### LinkedIn

Selectors confirmed against live LinkedIn DOM (April 2026):
- Title → `document.title` parsed: first segment before ` | `
- Title cleaning → strips ` - <URL>` suffix (LinkedIn sometimes embeds the job URL in the
  title segment as `"Job Title - https://linkedin.com/jobs/view/..."`) and strips ` - <company>`
  suffix when the company was found via the DOM (2-part titles: `"Job Title - Company | LinkedIn"`)
- Company → `document.title` second segment when 3-part (`"Title | Company | LinkedIn"`); falls back to first `a[href*="/company/"]` link in the DOM (handles 2-part titles like `"Title | LinkedIn"` on `/jobs/view/` pages)
- Location → `"Company: Location"` regex on `document.body.innerText` using both `·` (`\u00B7`) and `•` (`\u2022`); falls back to `"Location: N hours/days ago"` pattern when company is missing
- URL → `location.origin + location.pathname` (strips all tracking query params)
- Description → `[data-testid="expandable-text-box"]`

LinkedIn is a React app with hashed CSS class names — `document.title` parsing and body-text
search are more stable than CSS selectors that change with each deploy. `/jobs/view/` pages
only have 2-part titles so the company DOM fallback is essential there.

```javascript
javascript:(function(){var PORT=8000;if(!/linkedin\.com\/jobs/.test(location.href)){alert('Open a LinkedIn job posting first.');return;}function qs(s){return((document.querySelector(s)||{}).innerText||'').trim();}var liParts=document.title.split(' | ');var t=(liParts[0]||'').trim();var hi=t.lastIndexOf(' - http');if(hi>0)t=t.substring(0,hi).trim();var c=(liParts.length>=3&&(liParts[liParts.length-1]||'').trim()==='LinkedIn')?(liParts[1]||'').trim():'';if(!c){var cLinks=document.querySelectorAll('a[href*="/company/"]');for(var ci=0;ci<cLinks.length;ci++){var clt=(cLinks[ci].innerText||'').trim();if(clt&&clt.length>=2&&clt.length<=80&&!/^(LinkedIn|Sign in|Join now|Post a job|Post jobs for free)$/i.test(clt)){c=clt;break;}}}if(c){var di=t.lastIndexOf(' - ');if(di>0&&t.substring(di+3).trim().toLowerCase()===c.toLowerCase())t=t.substring(0,di).trim();}var url=location.origin+location.pathname;var loc='';if(c){var esc=c.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');var m=document.body.innerText.match(new RegExp(esc+'\\s*[\u00B7\u2022]\\s*([^\n\r\u00B7\u2022]+)'));if(m)loc=m[1].trim();}if(!loc){var lm=document.body.innerText.match(/([A-Za-z][^\u00B7\u2022\n\r]{2,50}?)\s*[\u00B7\u2022]\s*\d+\s*(?:hour|day|week|month|year)/i);if(lm)loc=lm[1].trim();}var desc=qs('[data-testid="expandable-text-box"]')||qs('.jobs-description__content .jobs-description-content__text')||qs('#job-details')||qs('.jobs-description');if(!t){var fb=document.title.split(' at ');t=(fb[0]||'').trim();if(!c)c=((fb[1]||'').split('|')[0]||'').trim();}if(!confirm('Add to tracker?\n\nTitle: '+t+'\nCompany: '+c+'\nLocation: '+loc+'\nURL: '+url))return;var p=new URLSearchParams({company:c,title:t,location:loc,url:url,source:'linkedin_manual',description:desc.slice(0,8000)});window.open('http://localhost:'+PORT+'/capture/add?'+p.toString(),'_blank','width=480,height=260');})();
```

### Wellfound

Selectors confirmed against live Wellfound DOM (April 2026):
- Container → `[data-test="JobListingSlideIn"]` (the right-panel job detail pane — stable `data-test`)
- Title → `[data-test="JobListingSlideIn"] h1` (NOT the first `h1` on the page, which is the search bar)
- Company → `img[alt^="Avatar for "]` inside the panel: alt is `"Avatar for CompanyName"` → strip prefix
- Location → `h1.nextElementSibling` (`<ul>` pill row after the job title) → text after `|` separator
- Description → `#job-description` inside the panel

The `/jobs?job_listing_slug=…` URL is a split-pane page. The first `h1` is the search bar and
the left sidebar has company links with "Promoted" badges — both wrong. Scoping to
`[data-test="JobListingSlideIn"]` isolates the selected job's detail panel reliably.

```javascript
javascript:(function(){var PORT=8000;var url=location.href;if(!/wellfound\.com/.test(url)){alert('Open a Wellfound job posting first.');return;}function qs(s){return((document.querySelector(s)||{}).innerText||'').trim();}var si=document.querySelector('[data-test="JobListingSlideIn"]');var t=si?qs('[data-test="JobListingSlideIn"] h1'):qs('h1');var wfImg=si?si.querySelector('img[alt^="Avatar for "]'):null;var c=wfImg?wfImg.alt.replace(/^Avatar for /,'').trim():'';if(!c&&si){var wfLinks=si.querySelectorAll('a[href*="wellfound.com/company/"]');for(var i=0;i<wfLinks.length;i++){var lt=(wfLinks[i].innerText||'').trim();if(lt){c=lt;break;}}}var wfH1=si?si.querySelector('h1'):null;var wfUl=wfH1?wfH1.nextElementSibling:null;var ulText=(wfUl&&wfUl.tagName==='UL')?(wfUl.innerText||'').trim():'';var locParts=ulText.split('|');var loc=locParts.length>1?locParts.slice(1).join('|').trim():locParts[0].trim();var desc=si?(si.querySelector('#job-description')||{innerText:''}).innerText.trim():qs('#job-description');if(!t||!c){var fb=(document.title.split('|')[0]||'').split(' at ');t=t||(fb[0]||'').trim();c=c||(fb[1]||'').trim();}if(!confirm('Add to tracker?\n\nTitle: '+t+'\nCompany: '+c+'\nLocation: '+loc+'\nURL: '+url))return;var p=new URLSearchParams({company:c,title:t,location:loc,url:url,source:'wellfound_manual',description:desc.slice(0,8000)});window.open('http://localhost:'+PORT+'/capture/add?'+p.toString(),'_blank','width=480,height=260');})();
```

### Welcome to the Jungle

Selectors confirmed against live WTTJ DOM (April 2026):
- Title → `[data-testid="job-metadata-block"] ~ h2` (it is an `h2`, not `h1`)
- Company → `[data-testid="job-metadata-block"] a[href*="/en/companies/"] span` (scoped to the job header to avoid the locale/country switcher in the nav)
- Location → text of `svg[alt="Location"]` chip + `svg[alt="Remote"]` chip
- Description → `[data-testid="job-section-description"]`

Falls back to `document.title` parsing (`"Title - COMPANY | Welcome to the Jungle"`) and URL slug if selectors miss.

```javascript
javascript:(function(){var PORT=8000;var url=location.href;if(!/welcometothejungle\.com/.test(url)){alert('Open a WTTJ job posting first.');return;}function qs(s){return((document.querySelector(s)||{}).innerText||'').trim();}function svgChip(alt){var el=document.querySelector('svg[alt="'+alt+'"]');return el?(el.parentElement.querySelector('span')||{innerText:''}).innerText.trim():'';}var t=qs('[data-testid="job-metadata-block"] ~ h2');var c=qs('[data-testid="job-metadata-block"] a[href*="/en/companies/"] span')||qs('[data-testid="job-metadata-block"] a[href*="/fr/entreprises/"] span');var locCity=svgChip('Location');var locRemote=svgChip('Remote');var loc=[locCity,locRemote].filter(Boolean).join(', ');var desc=qs('[data-testid="job-section-description"]');if(!t||!c){var parts=(document.title.split('|')[0]||'').split(' - ');t=t||(parts[0]||'').trim();c=c||(parts[parts.length-1]||'').trim();}if(!c){var m=url.match(/\/companies\/([^/]+)\//);if(m)c=m[1].replace(/-/g,' ');}if(!confirm('Add to tracker?\n\nTitle: '+t+'\nCompany: '+c+'\nLocation: '+loc+'\nURL: '+url))return;var p=new URLSearchParams({company:c,title:t,location:loc,url:url,source:'wttj_manual',description:desc.slice(0,8000)});window.open('http://localhost:'+PORT+'/capture/add?'+p.toString(),'_blank','width=480,height=260');})();
```

### HiringCafe

Selectors confirmed against live HiringCafe DOM (April 2026):
- Modal detection → `[role="dialog"][aria-modal="true"]` (Chakra UI slide-in panel opened when you click a job card)
- Title → first `h2` inside the modal
- Company → first `<span>` whose text starts with `@ ` — strip the prefix (e.g. `@ ClickUp` → `ClickUp`)
- Location → `<span>` sibling of the map-pin SVG (identified by path `10.5c0 7.142` in innerHTML)
- URL → `a[href^="/viewjob/"]` canonical link in the modal header → `https://hiring.cafe` + href
- Description → `<article>` element inside the modal (full rendered JD)

HiringCafe is a Next.js + Chakra UI app. Class names are Tailwind utility strings and may change;
`role="dialog"`, `aria-modal`, the `@ Company` span pattern, and `/viewjob/` links are stable.
**Click a job card first to open the detail panel before clicking the bookmarklet.**

```javascript
javascript:(function(){var PORT=8000;if(!/hiring\.cafe/.test(location.href)){alert('Open HiringCafe first.');return;}var d=document.querySelector('[role="dialog"][aria-modal="true"]');if(!d){alert('Click a job card to open the detail panel first.');return;}var h2=d.querySelector('h2');var t=h2?(h2.innerText||'').trim():'';var c='';var ss=d.querySelectorAll('span');for(var i=0;i<ss.length;i++){var st=(ss[i].innerText||'').trim();if(/^@\s/.test(st)){c=st.replace(/^@\s+/,'').trim();break;}}var loc='';var svgs=d.querySelectorAll('svg');for(var si=0;si<svgs.length;si++){if((svgs[si].innerHTML||'').indexOf('10.5c0 7.142')>=0){var lp=svgs[si].parentElement;var ls=lp?lp.querySelector('span'):null;if(ls)loc=(ls.innerText||'').trim();break;}}var vl=d.querySelector('a[href^="/viewjob/"]');var url=vl?'https://hiring.cafe'+vl.getAttribute('href'):location.href;var art=d.querySelector('article');var desc=art?(art.innerText||'').trim():'';if(!t){alert('No job title found — make sure the detail panel is open.');return;}if(!confirm('Add to tracker?\n\nTitle: '+t+'\nCompany: '+c+'\nLocation: '+loc+'\nURL: '+url))return;var p=new URLSearchParams({company:c,title:t,location:loc,url:url,source:'hiringcafe_manual',description:desc.slice(0,8000)});window.open('http://localhost:'+PORT+'/capture/add?'+p.toString(),'_blank','width=480,height=260');})();
```

---

## How it works

```text
Browser (bookmarklet click)
       │  confirm() dialog — verify title/company/location
       │
       │  window.open("http://localhost:8000/capture/add?company=...&title=...")
       │  (plain GET navigation — never blocked by mixed-content policy)
       ▼
harrier API (localhost:8000)
       │  parses query string → harrier.capture.add_captured_job
       ▼
tracker database  ←  scored + deduped, same pipeline as automated discovery
       │
       ▼
 Small result tab: "✅ Added: Acme — Senior Frontend Engineer"
 (with a ← back to job posting link)
```

The job goes through the exact same score_job plus build_tracker_row pipeline as
automated discovery: no special-casing (harrier.capture, spec 010).

---

## Update existing bookmarklet

If you already have the old `fetch`-based bookmarklet saved, just edit it:
1. Right-click the `+ Job` bookmark → **Edit**.
2. Replace the URL with the new `javascript:…` block above.
3. Save.

---
