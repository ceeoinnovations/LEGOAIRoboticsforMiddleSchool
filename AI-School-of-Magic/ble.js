export class BLEDevice {
    constructor() {
        this.device = null;
        this.server = null;
        this.service = null;
        this.writeCharacteristic = null;
        this.notifyCharacteristic = null;
        this.callback = null;
        this.disconnectCallback = null;
    }

    async connect(serviceUUID, writeUUID, notifyUUID) {
        try {
            this.device = await navigator.bluetooth.requestDevice({
                filters: [{ services: [serviceUUID] }]
            });

            this.handleDisconnect = this.handleDisconnect.bind(this);
            this.device.addEventListener('gattserverdisconnected', this.handleDisconnect);

            this.server  = await this.device.gatt.connect();
            this.service = await this.server.getPrimaryService(serviceUUID);
            this.writeCharacteristic  = await this.service.getCharacteristic(writeUUID);
            this.notifyCharacteristic = await this.service.getCharacteristic(notifyUUID);

            this.handleNotification = this.handleNotification.bind(this);
            await this.notifyCharacteristic.startNotifications();
            this.notifyCharacteristic.addEventListener(
                'characteristicvaluechanged',
                this.handleNotification
            );

            console.log('BLE connected:', this.device.name);
            return true;
        } catch (error) {
            console.error('BLE connect error:', error);
            return false;
        }
    }

    handleNotification(event) {
        const data = new Uint8Array(event.target.value.buffer);
        if (this.callback) {
            try { this.callback(data); }
            catch (err) { console.error('Notification callback error:', err); }
        }
    }

    handleDisconnect(event) {
        console.log('BLE disconnected:', this.device?.name);
        if (this.disconnectCallback) {
            try { this.disconnectCallback(event); }
            catch (err) { console.error('Disconnect callback error:', err); }
        }
    }

    async send(data) {
        if (!this.writeCharacteristic) {
            console.error('Not connected — cannot send.');
            return;
        }
        try {
            await this.writeCharacteristic.writeValue(new Uint8Array(data));
        } catch (error) {
            console.error('BLE send error:', error);
            throw error;
        }
    }

    disconnect() {
        if (this.device && this.device.gatt.connected) {
            this.device.gatt.disconnect();
        }
    }

    get name() {
        return this.device ? this.device.name : null;
    }
}
