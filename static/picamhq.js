
var centeredInterval = setInterval(getCentered, 200);
var reducedInterval = setInterval(getReduced, 200);


function getCentered() {
    let params = new URLSearchParams({command: 'centered'})
    fetch('/picamhq/uiCommand?' + params.toString())
	.then(response => response.json())  
	.then(json => {
	    var image = document.getElementById('centered');
	    image.src = 'data:image/jpeg;base64,'+json['image'];
            var fs = document.getElementById('focus_score');
            fs.textContent = json['focus_score'];
	})
}

function getReduced() {
    let params = new URLSearchParams({command: 'reduced'})
    fetch('/picamhq/uiCommand?' + params.toString())
	.then(response => response.json())  
	.then(json => {
	    var image = document.getElementById('reduced');
	    image.src = 'data:image/jpeg;base64,'+json['image'];
	})
}
