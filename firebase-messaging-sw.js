importScripts('https://www.gstatic.com/firebasejs/10.8.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.8.0/firebase-messaging-compat.js');

const firebaseConfig = {
  apiKey: "AIzaSyCwGFWUeGeAnVew0MawvtYjLqQrKDhLcXE",
  authDomain: "scanty-meals.firebaseapp.com",
  projectId: "scanty-meals",
  storageBucket: "scanty-meals.firebasestorage.app",
  messagingSenderId: "441632174969",
  appId: "1:441632174969:web:4218799226a1179694b5a8",
  measurementId: "G-VGFEC6VZ2T"
};

firebase.initializeApp(firebaseConfig);
const messaging = firebase.messaging();

messaging.onBackgroundMessage((payload) => {
  console.log('[firebase-messaging-sw.js] Received background message ', payload);
  
  const notificationTitle = payload.notification?.title || payload.data?.title || 'Scanty Meals';
  const notificationOptions = {
    body: payload.notification?.body || payload.data?.message || 'You have a new update.',
    icon: '/IMG/favicon.jpeg',
    badge: '/IMG/favicon.jpeg',
    data: { url: payload.data?.url || '/' }
  };
  
  self.registration.showNotification(notificationTitle, notificationOptions);
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  const urlToOpen = new URL(event.notification.data.url, self.location.origin).href;
  
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      let matchingClient = null;
      for (let i = 0; i < windowClients.length; i++) {
        const windowClient = windowClients[i];
        if (windowClient.url === urlToOpen) {
          matchingClient = windowClient;
          break;
        }
      }
      if (matchingClient) {
        return matchingClient.focus();
      } else {
        return clients.openWindow(urlToOpen);
      }
    })
  );
});
