public class StSampleFormTriggerHandler {
    Map<String, Id> formIdentifiers = new Map<String, Id>();
    public void andFinally() {
        StSampleRecordUtility.updateSampleRecordLinks(formIdentifiers, 'Form');
    }
}
