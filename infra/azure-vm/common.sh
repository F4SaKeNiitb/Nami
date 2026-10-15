# Shared by the scripts that talk to the VM. Same names as deploy.sh derives. Operations go through `az vm run-command` (HTTPS to Azure's
# management plane), not SSH: it works from networks that block port 22 (found live), and the VM needs no SSH port open at all.
RG="${RG:-nami-rg}"; NAME="${NAME:-nami}"; LOCATION="${LOCATION:-centralindia}"
SUB=$(az account show --query id -o tsv)
SUFFIX=$(printf '%s%s' "$SUB" "$NAME" | shasum | cut -c1-6)
DNS="$NAME-$SUFFIX"; KV="$NAME-kv-$SUFFIX"; SA="${NAME}blob${SUFFIX}"; FQDN="$DNS.$LOCATION.cloudapp.azure.com"

# Run a shell script on the VM as root and print its output.  vm_run 'uptime'
vm_run() {
  az vm run-command invoke -g "$RG" -n "$NAME-vm" --command-id RunShellScript --scripts "$1" --query "value[0].message" -o tsv
}
