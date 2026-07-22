// static/js/chat_module.js

function switchMsgTab(tab) {
    ['partners', 'recv', 'sent'].forEach(t => {
        document.getElementById('list-' + t).style.display = (t === tab) ? 'block' : 'none';
        const tabEl = document.getElementById('tab-' + t);
        if(t === tab) {
            tabEl.classList.remove('blue-pill-inactive');
            tabEl.classList.add('blue-pill-active');
        } else {
            tabEl.classList.remove('blue-pill-active');
            tabEl.classList.add('blue-pill-inactive');
        }
    });
}

function openMsgModalWithReceiver(targetUser, currentUser) {
    if(targetUser === currentUser) return; 
    const modal = document.getElementById('msgModal');
    const receiverSelect = document.getElementById('msgReceiver');
    receiverSelect.value = targetUser;
    modal.style.display = 'block';
}

function openChat(partnerName) {
    const popupWidth = 450; 
    const popupHeight = 650;
    const left = (window.screen.width / 2) - (popupWidth / 2);
    const top = (window.screen.height / 2) - (popupHeight / 2);
    window.open(`/chat_popup/${encodeURIComponent(partnerName)}`, `chat_${partnerName}`, `width=${popupWidth},height=${popupHeight},left=${left},top=${top},menubar=no,toolbar=no`);
}

async function sendNewMessage() {
    const receiver = document.getElementById('msgReceiver').value;
    const content = document.getElementById('msgContent').value;
    const fileInput = document.getElementById('msgFile');
    
    if(!receiver) return;
    
    let formData = new FormData();
    formData.append('receiver', receiver);
    formData.append('content', content);
    if (fileInput.files.length > 0) formData.append('file', fileInput.files[0]);
    
    try {
        const res = await fetch('/send_message', { method: 'POST', body: formData });
        if (res.ok) { 
            document.getElementById('msgModal').style.display = 'none'; 
            document.getElementById('msgContent').value = '';
            openChat(receiver); 
        }
    } catch (error) { console.error("쪽지 전송 실패:", error); }
}

function updateMessageBadge() {
    fetch('/api/unread_messages')
        .then(res => res.json())
        .then(data => {
            const mainBadge = document.getElementById('left-unread-badge');
            if (mainBadge) {
                mainBadge.innerText = data.total_unread;
                mainBadge.style.display = data.total_unread > 0 ? 'block' : 'none';
            }
            const partnerBadges = document.querySelectorAll('.partner-badge');
            partnerBadges.forEach(badge => {
                const partnerName = badge.getAttribute('data-partner');
                const count = data.details[partnerName] || 0;
                badge.innerText = count;
                badge.style.display = count > 0 ? 'inline-block' : 'none';
            });
        });
}

function toggleEmojiPicker(pickerId) {
    const picker = document.getElementById(pickerId);
    picker.style.display = (picker.style.display === 'none' || picker.style.display === '') ? 'grid' : 'none';
}

function addEmoji(inputId, emoji) {
    const input = document.getElementById(inputId);
    input.value += emoji;
    input.focus();
    document.getElementById('newMsgEmojiPicker').style.display = 'none';
}

// 자동 갱신 셋업
document.addEventListener('DOMContentLoaded', () => {
    setInterval(updateMessageBadge, 10000); 
});