'use strict';
const navButton=document.createElement('button');
navButton.type='button';
navButton.textContent='我的 @ / Codex';
navButton.onclick=()=>{
  const key=location.hash.slice(1)||sessionStorage.getItem('teamup-console');
  if(key)sessionStorage.setItem('teamup-local',key);
  location.assign('/');
};
document.querySelector('nav[aria-label="Client navigation"]')?.append(navButton);
