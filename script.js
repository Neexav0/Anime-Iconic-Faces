
const characters = [
  {
    name: "Makima",
    series: "Chainsaw Man",
    nickname: "Control Devil",
    description: "A calm, hypnotic manipulator whose mysterious smile hides terrifying authority.",
    accent: "#ff3b5f",
    video: "videos/makima.mp4",
    x: 15, y: 19, size: "2.5rem", rot: "-8deg"
  },
  {
    name: "Mikasa Ackerman",
    series: "Attack on Titan",
    nickname: "The Strongest Soldier",
    description: "Silent, fiercely loyal, and one of humanity's deadliest warriors.",
    accent: "#d14b4b",
    video: "videos/mikasa.mp4",
    x: 70, y: 17, size: "2.2rem", rot: "6deg"
  },
  {
    name: "Yor Forger",
    series: "Spy x Family",
    nickname: "Thorn Princess",
    description: "An elegant assassin who balances extraordinary combat skills with a caring heart.",
    accent: "#ff4568",
    video: "videos/yor.mp4",
    x: 12, y: 40, size: "2.2rem", rot: "-6deg"
  },
  {
    name: "Shinobu Kocho",
    series: "Demon Slayer",
    nickname: "Insect Hashira",
    description: "A graceful and sharp-witted swordswoman whose butterfly-like elegance hides formidable skill.",
    accent: "#bd8cff",
    video: "videos/shinobu.mp4",
    x: 84, y: 31, size: "2.1rem", rot: "5deg"
  },
  {
    name: "Mitsuri Kanroji",
    series: "Demon Slayer",
    nickname: "Love Hashira",
    description: "A warm-hearted and powerful swordswoman who leads with compassion and courage.",
    accent: "#ff8fc8",
    video: "videos/mitsuri.mp4",
    x: 17, y: 76, size: "2.1rem", rot: "4deg"
  },
  {
    name: "Marin Kitagawa",
    series: "My Dress-Up Darling",
    nickname: "The Cosplay Queen",
    description: "A cheerful, confident anime enthusiast whose passion for cosplay and love for self-expression make her unforgettable.",
    accent: "#ff8fb8",
    video: "videos/marin.mp4",
    x: 80, y: 68, size: "2rem", rot: "-4deg"
  },
  {
    name: "Roronoa Zoro",
    series: "One Piece",
    nickname: "King of Hell",
    description: "A relentless swordsman whose ambition and three-sword style make every edit legendary.",
    accent: "#35e0a1",
    video: "videos/zoro.mp4",
    x: 50, y: 85, size: "2.4rem", rot: "-3deg"
  },
  {
    name: "Nico Robin",
    series: "One Piece",
    nickname: "Devil Child",
    description: "An elegant archaeologist whose quiet intellect hides immense emotional depth.",
    accent: "#9b5cff",
    video: "videos/robin.mp4",
    x: 29, y: 61, size: "2.3rem", rot: "-5deg"
  },
  {
    name: "Hinata Hyuga",
    series: "Naruto",
    nickname: "The Sunflower Princess",
    description: "Gentle yet courageous, she grows into one of the Hidden Leaf's brightest spirits.",
    accent: "#b993ff",
    video: "videos/hinata.mp4",
    x: 73, y: 49, size: "2rem", rot: "8deg"
  },
  {
    name: "Waguri",
    series: "Oshi no Ko",
    nickname: "Golden Blossom",
    description: "A graceful presence whose warmth and quiet charm leave a lasting impression.",
    accent: "#ff8fb8",
    video: "videos/waguri.mp4",
    x: 47, y: 36, size: "2.6rem", rot: "-2deg"
  }
];

const names = document.getElementById("names");
const panel = document.getElementById("info");
const title = document.getElementById("title");
const series = document.getElementById("series");
const nick = document.getElementById("nick");
const desc = document.getElementById("desc");
const cursor = document.getElementById("cursorGlow");
const videoLayer = document.getElementById("videoLayer");
const musicBtn = document.getElementById("musicBtn");

const nameElements = [];
const characterElements = new Map();

let activeVideo = null;
let selectedCharacter = null;
let previewCharacter = null;
let characterAudioOn = false;

function layoutNames() {
  const mobile = window.innerWidth <= 600;
  const tablet = window.innerWidth <= 900;

  const desktopPositions = [
    [15, 19],
    [50, 58],
    [12, 40],
    [84, 31],
    [17, 76],
    [80, 68],
    [50, 85],
    [29, 61],
    [73, 49],
    [47, 36]
  ];

  const tabletPositions = [
    [18, 17],
    [50, 58],
    [17, 34],
    [80, 32],
    [19, 73],
    [79, 69],
    [51, 84],
    [29, 57],
    [74, 49],
    [49, 36]
  ];

  const mobilePositions = [
    [25, 15],
    [75, 15],
    [25, 26],
    [75, 26],
    [25, 37],
    [75, 37],
    [25, 48],
    [75, 48],
    [25, 59],
    [75, 59]
  ];

  const positions = mobile
    ? mobilePositions
    : tablet
      ? tabletPositions
      : desktopPositions;

  nameElements.forEach((el, i) => {
    const [x, y] = positions[i];
    el.style.left = `${x}%`;
    el.style.top = `${y}%`;
  });
}

function stopActiveVideo() {
  if (activeVideo) {
    activeVideo.classList.remove("active");
    activeVideo.pause();
    activeVideo = null;
  }
}

function showCharacter(character, element) {
  document.querySelectorAll(".name").forEach(el => {
    el.classList.toggle("active", el === element);
  });

  document.body.classList.add("name-focused");

  document.documentElement.style.setProperty(
    "--accent",
    character.accent
  );

  cursor.style.background =
    `radial-gradient(circle, ${character.accent}44, transparent 70%)`;

  title.textContent = character.name;
  series.textContent = character.series;
  nick.textContent = character.nickname;
  desc.textContent = character.description;

  panel.style.borderColor = `${character.accent}77`;
  panel.classList.add("show");

  if (activeVideo !== characterElements.get(character).video) {
    stopActiveVideo();

    const video = characterElements.get(character).video;

    if (video) {
      activeVideo = video;
      video.classList.add("active");
      video.muted = !characterAudioOn;

      video.play().catch(() => {
      });
    }
  } else if (activeVideo) {
    activeVideo.muted = !characterAudioOn;
  }
}

function clearCharacter() {
  selectedCharacter = null;
  previewCharacter = null;

  document.querySelectorAll(".name").forEach(el => {
    el.classList.remove("active");
  });

  document.body.classList.remove("name-focused");
  panel.classList.remove("show");

  cursor.style.background =
    "radial-gradient(circle, rgba(255,77,109,.22), transparent 70%)";

  stopActiveVideo();
}

function preview(c, el) {
  if (selectedCharacter) return;

  previewCharacter = c;
  showCharacter(c, el);
}

characters.forEach((c, i) => {
  const el = document.createElement("div");

  el.className = "name";
  el.textContent = c.name;
  el.style.fontSize = c.size;
  el.style.setProperty("--rot", c.rot);
  el.style.setProperty("--color", c.accent);
  el.style.setProperty("--delay", `${i * -0.35}s`);
  el.setAttribute("role", "button");
  el.setAttribute("tabindex", "0");
  el.setAttribute("aria-label", `Explore ${c.name}`);
  el.setAttribute("aria-pressed", "false");

  names.appendChild(el);
  nameElements.push(el);

  let vid = null;

  if (c.video) {
    vid = document.createElement("video");
    vid.src = c.video;
    vid.loop = true;
    vid.muted = true;
    vid.volume = 0.72;
    vid.preload = "none";
    vid.playsInline = true;
    vid.setAttribute("playsinline", "");
    vid.setAttribute("aria-hidden", "true");

    videoLayer.appendChild(vid);
  }

  characterElements.set(c, { element: el, video: vid });

  el.addEventListener("mouseenter", () => {
    preview(c, el);
  });

  el.addEventListener("mouseleave", () => {
    if (!selectedCharacter && previewCharacter === c) {
      clearCharacter();
    }
  });

  el.addEventListener("focus", () => {
    preview(c, el);
  });

  el.addEventListener("blur", () => {
    if (!selectedCharacter && previewCharacter === c) {
      clearCharacter();
    }
  });

  el.addEventListener("click", e => {
    e.stopPropagation();

    selectedCharacter = c;
    previewCharacter = null;

    document.querySelectorAll(".name").forEach(n => {
      n.setAttribute("aria-pressed", String(n === el));
    });

    showCharacter(c, el);
  });

  el.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      el.click();
    }
  });
});

layoutNames();

window.addEventListener("resize", layoutNames);

document.addEventListener("click", e => {
  if (
    !e.target.closest(".name") &&
    !e.target.closest("#info") &&
    !e.target.closest(".controls")
  ) {
    clearCharacter();
  }
});

document.addEventListener("pointermove", e => {
  if (e.pointerType === "touch") return;

  cursor.style.left = `${e.clientX}px`;
  cursor.style.top = `${e.clientY}px`;
});

if (window.matchMedia("(pointer: coarse)").matches) {
  cursor.style.display = "none";
}

const petals = document.getElementById("petals");

for (let i = 0; i < 42; i++) {
  const p = document.createElement("div");

  p.className = "petal";
  p.style.left = `${Math.random() * 100}%`;
  p.style.animationDuration = `${10 + Math.random() * 12}s`;
  p.style.animationDelay = `${Math.random() * -20}s`;
  p.style.opacity = 0.2 + Math.random() * 0.5;

  petals.appendChild(p);
}

musicBtn.addEventListener("click", e => {
  e.stopPropagation();

  characterAudioOn = !characterAudioOn;

  musicBtn.textContent = characterAudioOn
    ? "♫ Character Audio On"
    : "♫ Character Audio Off";

  musicBtn.setAttribute(
    "aria-pressed",
    String(characterAudioOn)
  );

  if (activeVideo) {
    activeVideo.muted = !characterAudioOn;

    if (characterAudioOn) {
      activeVideo.play().catch(() => {});
    }
  }
});
