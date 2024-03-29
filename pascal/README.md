To start the rabbit docker image
```bash
docker run -it --rm --name rabbitmq -p 5672:5672 -p 15672:15672 rabbitmq:3.13-management
```

In powershell for running in background
```powershell
$job = Start-Job { docker run --rm --name rabbitmq -p 5672:5672 -p 15672:15672 rabbitmq:3.13-management }
```

Some operations
```powershell
Get-Job # for findding the job name of the docker
Stop-Job <Job_ID># for stopping the docker background
# the container could be stopped in the GUI too
```